# Plan

Master plan for stabilising Morphe Builder. Comes out of the code review done on
2026-10-01. Line references are to the working tree at that date.

## 1. Scraper resilience

- [x] Relaunch the browser and retry once when the page or browser has died
- [x] Send scraper logs to the build console
- [x] Read the newest version from the search results (one page load, not three)
- [x] Restore the original search URL (drop the filter/quoting change)
- [x] Recover when the browser fails to launch at server startup (done 2026-10-05:
      retry on the next task after a 60 s cooldown; not run with a real browser)
- [ ] A browser task that hangs forever still blocks the worker (no watchdog)
- [x] Bump cloakbrowser 0.3.31 -> 0.5.12, keyless (done 2026-10-05; not yet run
      with a real browser or in Docker)
- [ ] Use a cloakbrowser licence key when one is set in Settings (see step 5)
- [ ] Find out why the page crashes inside the container (memory limit, `docker stats`)
- [ ] Verify a full build (download + patch) and a run inside Docker (see step 7)

## 1a. Bring the scraper changes up to the project Python rules

Decided 2026-10-01: retrofit first, then continue with step 2 under the same rules.

Done 2026-10-02 by a subagent, reviewed and verified.

- [x] Add pytest, ruff and mypy as dev dependencies in `pyproject.toml`
- [x] Add a test output folder to `.gitignore` if tests write files (not needed:
      tests use `tmp_path`)
- [x] pytest tests for `_goto` recovery (page crash, browser closed, unrelated error
      re-raised), with the browser mocked
- [x] pytest tests for `latest_version` and `_extract_first_valid_row` against saved
      search-page HTML
- [x] pytest test that scraper logs follow a swapped `sys.stdout`
- [x] pytest test for the APKMirror fallback in `get_supported_versions`, scraper mocked
- [x] Type hints and docstrings (args, returns, raises) on the new and changed functions
- [x] Catch specific exceptions in the recovery path instead of `except Exception`
- [x] Lines within 88 characters; ruff and mypy clean on the changed code

Left for later, outside the retrofit scope (see step 6):

- The rest of `src/apkmirror_downloader.py` still has 9 ruff findings (blind
  `except Exception`, unsorted imports), 10 lines over 88 characters, and no type
  hints or docstrings on `search`, `download_file` and the other older methods.
- mypy reports 6 findings in total: missing stubs for `yaml`, `cloakbrowser` and
  `apscheduler`, and one untyped variable in `src/main.py`.

## 2. Add-source flow

All work from here on follows the project Python rules, with pytest tests.

- [x] Keep the inline sync so the patch list exists before the page re-renders
- [x] Normalise and validate the repo input (`github.com/...`, `.git`, casing, extra path)
- [x] Show add-source errors in the UI instead of ignoring the response
- [x] Stay on the Discovery tab after adding a source
- [x] Show real app names in Discovery instead of package IDs (`src/patcher.py:952`)
- [x] Make "Sync Sources" bypass the 6-hour throttle
- [x] Remove or fix the "no releases" fallback that downloads only the JSON (kept;
      `add_source` reports the missing `.mpp`)
- [x] Surface GitHub rate-limit failures (in add-source, the build log and after
      "Sync Sources"; UI notice not yet checked in a browser)

## 3. Config safety

- [x] One locked, atomic load-modify-save for `config.yaml` (`src/config_store.py`)
- [x] Stop `run_pipeline` overwriting UI changes made during a build

Done 2026-10-02 by a subagent, independently reviewed. Not yet run
against a live server or inside Docker.

## 4. Other bugs from the review

- [ ] Patches hidden across sources: dedup is by name only (decided 2026-10-05: the
      saved selection carries the source of each patch; design under investigation)
- [x] Obtainium misses patch updates: version string always uses the Morphe tag
      (now `<app version>-<patch tag>-<hash>` over the sources used and the selection)
- [x] A failed patch leaves no APK: old file is deleted first (now patched in
      `apks/.building/` and moved into place only on success)
- [x] `/download/...` returns `null` with 200 instead of 404
- [x] Unsafe upload path: package id and version are validated, HTTP 400 otherwise
- [x] No authentication, CORS `*` (decided 2026-10-05: not doing; the whole app
      stays open)
- [x] Version choice varies between runs: providers now taken in config order, base
      source recognised by repo `MorpheApp/morphe-patches`
- [x] App names with an apostrophe break the pipeline card
- [x] Changing an app's patch selection does not trigger a rebuild: `run_pipeline`
      skips when app version and patch tag are unchanged and the APK exists
      (fixed 2026-10-02: the selection is recorded in `last_successful_build.patches`
      and compared)
- [x] Two `save_settings` calls at once can leave the scheduler on the older cron
      schedule (now scheduled from the file under a lock)
- [x] `/download/<package id>` matches by prefix, so `com.foo` can serve an APK of
      `com.foobar`
- [x] `/api/apps/{id}` answers 200 with an error body for an unknown app, and apps
      without `obtainium_id` raise `KeyError`
- [x] The frontend recognised the base source by the display name "Morphe Patches"
      (now by an `is_base` flag from the backend, decided by repo)
- [x] `add_app` did not validate the package id (now HTTP 400 like the upload
      route, nothing written, error shown in the UI)
- [x] A corrupt patch JSON silently dropped that source in `get_supported_versions`
      (now `PatchListError`: error on the app's card, previous APK kept, 502 in the
      patch dialog; downloads are atomic)
- [ ] One unreadable patch list stops every unpinned app and the patch dialog for
      every app; narrow it to the apps that use the source once the selection names
      its source
- [x] A sync did not repair an unreadable patch list (now downloaded again on any
      sync; a list that is still unreadable afterwards is not reported as an update)
- [x] `patch_app` passed all bundles before all `-e`/`-d` selectors (fixed
      2026-10-05: each bundle is followed by its own selectors). Verified
      2026-10-05 with the released jar v1.18.1: selectors apply to the bundle they
      follow, so multi-source builds apply the selection to the last bundle only
      and still exit 0
- [x] Apps built from several sources before the grouping fix keep their
      incomplete APK until an input changes; decide on a one-off forced rebuild
- [x] Patch names are matched to sources by exact case, the CLI ignores case; a
      name with different case falls to the base bundle and is lost
- [x] The CLI exits 0 and writes an APK when no patch was applied or a selected
      name is unknown; `patch_app` reports that as a successful build
- [x] The first release asset ending in `.json` is taken as the patch list. A
      source that ships other JSON files (found 2026-10-05 with
      `SysAdminDoc/Hushfacebook`: an SBOM, a tooling file and a receipt) gets the
      wrong file, counts as unreadable and is "repaired" with the same file. The
      real list is `patches-list.json` in the repo at the release tag. Fix: accept
      only an asset that is a patch list, otherwise use the repo fallback
      (fixed 2026-10-05; the repair path was run against the real source in a
      scratch folder: 60 patches loaded)
- [ ] The download progress bar shows more than 100% for compressed responses
- [ ] After that fix: check that the Hushfacebook patches build against Facebook
      with real `morphe-cli`
- [ ] A source without releases reports "updated" on every sync, so its apps are
      rebuilt on every build (older than this work; found in review 2026-10-05,
      read from the code, not run)
- [ ] `REPO_PATTERN` accepts `.` and `..` as owner or repo, so a source can point
      the GitHub API call at another path; fix before the token is sent (step 5)
- [x] `POST /api/ui/upload_apk` and `GET /api/ui/discovery?sync=true` can be
      triggered by any web page the user visits (security review 2026-10-05;
      Origin check, Host allow-list and sync as POST done 2026-10-05)
- [x] Guard fixes from the second security review: proxy lock-out, framing,
      loopback server URL trusted as Origin (done 2026-10-05, not re-reviewed)
- [x] `GET /api/ui/updates` and `/api/ui/source_updates` can be fired many times
      by another site and use up the GitHub limit; one refresh at a time
- [x] Third security review fixes: full origins in `ALLOWED_HOSTS`, delete
      `/api/ui/source_updates`, cache stamp at completion, framing headers on 500s
      (done 2026-10-05, not re-reviewed)
- [ ] UI buttons other than Save Settings ignore a refused write
- [ ] A scrape that fails is retried on every updates refresh

The three ticked build items above (forced rebuild, case, empty builds) were fixed
2026-10-05: `BUILD_LAYOUT` in the version hash, `PatchSelectionError`.

The three fixes above were done 2026-10-05 by a subagent and independently reviewed
twice. Not checked in a browser.

None of the step 4 fixes has been run against real `morphe-cli`, a live server or
Docker yet.

## 5. GitHub API token

Added 2026-10-02 on request. Unauthenticated calls are limited to 60 per hour per IP;
with a token the limit is 5,000 per hour.

- [x] Decide where the token comes from (decided 2026-10-05: a Settings field that
      writes to `.env`; never `config.yaml`)
- [x] Send the token as `Authorization: Bearer ...` on the `api.github.com` call
      (`check_and_download_release`, the only one left; done 2026-10-05, not
      reviewed, no real token used)
- [x] Never log, print or return the token (global redaction, 2026-10-05)
- [x] Report a rejected token (401) clearly instead of as a generic sync failure
- [x] pytest tests: header sent when set, absent when unset, 401 reported
- [x] Independent code and security review of the token step (done 2026-10-06;
      findings fixed, see section 8)
- [ ] Show only "token set / not set" in the UI (secrets step 4, section 8)
- [ ] Show the token-permissions warning in the UI
- [ ] Document the variable in `README.md` and pass it through in the Docker setup
- [ ] Try a real token end to end

## 6. Cleanup (only if asked)

- [ ] `test.py` is the old scraper and imports an uninstalled package
- [x] `/api/ui/source_updates` is never called by the frontend (deleted 2026-10-05)
- [ ] Dockerfile installs a second Chromium that is not used
- [ ] Bring the older code in `src/` up to the Python rules (ruff, mypy stubs or
      overrides, type hints, docstrings, bare `except:` sites)
- [ ] `latest_version` takes the first non-alpha/beta row as-is; for YouTube that is
      currently `21.39.522-SECONDARY`. Decide whether such variants should be skipped.

## 7. Docker end-to-end test

Added 2026-10-02 on request. Blocked on the development machine: no container runtime
is installed (`docker`, `podman`). Run it before the next deploy; the build-path
changes of steps 3 and 4 are so far covered only by tests with the patcher faked.

- [ ] Get a runtime: install Docker or Podman here, or run the test on the machine
      that hosts the container
- [ ] `docker build` succeeds and the image starts; scraper worker and tool sync come
      up, Java and Scraper show green
- [ ] `/data` mounted as a directory: `config.yaml` is created and survives a restart
- [ ] One real build through the API: download from APKMirror, patch with
      `morphe-cli`, APK lands in `apks/patched/` as
      `<pkg>-<version>-<tag>-<hash>.apk`, `apks/.building/` is empty afterwards
- [ ] `/view/<id>`, `/api/apps` and `/download/<file>` serve the build; an unknown
      name gives 404
- [ ] A second build is skipped; changing the patch selection rebuilds and changes
      the version string
- [ ] A failing patch leaves the previous APK served
- [ ] Add the result to Obtainium and confirm it offers the rebuilt APK as an update
- [ ] Watch `docker stats` during the build to pin down the page crash (step 1)

## 8. Secrets, request guards, cloakbrowser key

Approved 2026-10-05. Decisions and reasons are in `decisions.md`. Work is on
branch `secrets-guards-worker`.

- [x] Step 0: cloakbrowser 0.5.12, keyless
- [x] Step 1: secret store (`DATA_DIR/.env`) and `/api/ui/secrets` routes; CORS
      removed
- [x] Step 1b to 1e: Host allow-list, Origin check, no framing, sync as POST, one
      updates refresh at a time, `/api/ui/source_updates` deleted
- [x] Step 2: scraper worker recovery, shutdown, degraded updates, browser state
- [x] Before step 3: repo name rule, environment secrets validated and removed
      from the process environment, global redaction, Java without the secrets
- [x] Step 3: GitHub token (section 5)
- [x] Review everything listed as not reviewed in `todo.md` (done 2026-10-06:
      three reviews, three fix lanes, a second review, a follow-up fix round;
      the follow-up round is not reviewed, nothing committed yet)
- [x] Slash tags, staged release pair, one build and one sync at a time, guard
      and secret fixes from the reviews (2026-10-06)
- [x] Server URL stays a trust anchor until `ALLOWED_HOSTS` lists a full origin;
      an empty server URL can be saved (decided 2026-10-06)
- [x] Tailwind and Alpine no longer load from a CDN (done 2026-10-06, merged into
      `main`: stylesheet built ahead of time with the Tailwind
      3.4.17 standalone CLI and committed, Alpine 3.17.4 vendored, both served
      from `/static`; independently reviewed; not checked in a browser)
- [ ] Step 4: Settings UI for both secrets, with the `environment_invalid` state
      and the token-permissions warning
- [ ] Step 5: licence key passed to the browser explicitly; keyless fallback when
      rejected; relaunch on a key change; security review afterwards
- [ ] Step 6: docs (README: `ALLOWED_HOSTS`, proxy must pass `Host`, both
      variables, zero-permission token; codemaps; `architecture.md`)
- [ ] Step 7: upload keeps the metadata when the icon is missing

## 9. Per-source patch selection

Approved 2026-10-05; design in `decisions.md`.

- [x] Step 1: each bundle followed by its own selectors; forced rebuild; empty
      builds fail; names match ignoring case
- [ ] Step 2: backend reads both selection shapes; bare names resolved at build
      time; a source is a provider only when its own patches are selected; narrow
      "one unreadable list blocks every app"
- [ ] Step 3: API and dialog per source, tabs keyed by repo, mapping saved and
      validated

## 10. Verify with real tools

Nothing on the branch has been run against a real browser, real `morphe-cli`, a
live server, a reverse proxy or Docker. Exceptions: a first sync and a repair of
the real `SysAdminDoc/Hushfacebook` and `MorpheApp/morphe-patches` sources, run
without a token in a scratch folder.

- [ ] Server starts from the branch; browser launches on cloakbrowser 0.5.12
- [ ] Build Facebook with the Hushfacebook patches
- [ ] A multi-source build applies the selection of every bundle
- [ ] Browser check of the UI changes
- [ ] Reverse proxy with the full origin in `ALLOWED_HOSTS`
- [ ] Docker end-to-end test (section 7)
