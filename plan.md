# Plan

Master plan for stabilising Morphe Builder. Comes out of the code review done on
2026-10-01. Line references are to the working tree at that date.

## 1. Scraper resilience

- [x] Relaunch the browser and retry once when the page or browser has died
- [x] Send scraper logs to the build console
- [x] Read the newest version from the search results (one page load, not three)
- [x] Restore the original search URL (drop the filter/quoting change)
- [ ] Recover when the browser fails to launch at server startup (`src/main.py:65`)
- [ ] Decide on bumping cloakbrowser 0.3.31 -> 0.5.x
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

- [ ] Patches hidden across sources: dedup is by name only (needs a decision: the
      saved selection would have to carry the source)
- [x] Obtainium misses patch updates: version string always uses the Morphe tag
      (now `<app version>-<patch tag>-<hash>` over the sources used and the selection)
- [x] A failed patch leaves no APK: old file is deleted first (now patched in
      `apks/.building/` and moved into place only on success)
- [x] `/download/...` returns `null` with 200 instead of 404
- [x] Unsafe upload path: package id and version are validated, HTTP 400 otherwise
- [ ] No authentication, CORS `*` (needs a decision on the mechanism)
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
- [ ] The frontend still recognises the base source by the display name
      "Morphe Patches" (`index.html`), so a Morphe source re-added through the UI as
      "MorpheApp Patches" is treated as third-party in the patch dialog
- [ ] `add_app` does not validate the package id; an id with an apostrophe breaks
      the pipeline card the same way names did
- [ ] A corrupt patch JSON silently drops that source in `get_supported_versions`
      (bare `except`)

None of the step 4 fixes has been run against real `morphe-cli`, a live server or
Docker yet.

## 5. GitHub API token

Added 2026-10-02 on request. Unauthenticated calls are limited to 60 per hour per IP;
with a token the limit is 5,000 per hour.

- [ ] Decide where the token comes from (to be settled when this step starts). The
      project rules keep secrets in `.env` / environment variables only, which rules
      out `config.yaml` and a plain Settings field. Options: `GITHUB_TOKEN` env var
      only, or a Settings field that writes to `.env`.
- [ ] Send the token as `Authorization: Bearer ...` on every `api.github.com` call
      (`check_and_download_release`, `check_source_updates`)
- [ ] Never log, print or return the token; show only "token set / not set" in the UI
- [ ] Report a rejected token (401) clearly instead of as a generic sync failure
- [ ] Document the variable in `README.md` and pass it through in the Docker setup
- [ ] pytest tests: header sent when set, absent when unset, 401 reported

## 6. Cleanup (only if asked)

- [ ] `test.py` is the old scraper and imports an uninstalled package
- [ ] `/api/ui/source_updates` is never called by the frontend
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
