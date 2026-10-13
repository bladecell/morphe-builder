# Todo

Current working checklist. The full list lives in `plan.md`.

## State 2026-10-14: commit checkpoint

- [x] Full suite: 1709 passed, 1 skipped, 1 xfailed. Updated the stale header-badge
      assertion to match the requested removal; no runtime change was needed.
- [x] Commit preflight found no confirmed introduced secrets or runtime artifacts;
      diff whitespace checks passed.
- Docker/Facebook verification remains unresolved: latest CloakBrowser 0.6.2 and
  0.3.31 both selected the same Chromium binary; Facebook results loaded on the
  host but remained on verification in Docker. Experimental dependencies/images
  did not change the production Dockerfile or project lockfile.
- The general per-app minimum SDK setting remains proposed, not implemented.

## State 2026-10-12: isolated Docker validation

- [x] Image build, Java/browser startup, rendered UI/API/static checks and settings
      persistence across restart passed using a fresh named volume and localhost.
- [x] Added HushFacebook through the API; selected Facebook 582.0.0.50.54 and
      attempted a real build. Container remained healthy; no APK was patched.
- [ ] Real download/patch/artifact checks are blocked: an isolated search probe
      returned HTTP 403 and a Cloudflare verification page on APKMirror.
- Test container is stopped; image and isolated volume are retained. Production
  data and credentials were not used. See plan section 7 for names and evidence.

## State 2026-10-11: HushFacebook artifact-floor correction

- [x] Build-time SDK inspection and patching use one private snapshot; an upload
      atomically replacing the raw-cache path cannot change the validated input.
- [x] Upload promotion rechecks current provider policy inside the config write lock.
- [x] Cached inspector failures fail without redownload or deletion; failed uploads
      preserve prior raw and patched APKs. Full suite: 1701 passed, 1 xfailed.
- [x] APKEditor 1.4.9 inspected two local Facebook APKs as minSdk 28; no API-30
      artifact was available to validate a complete build.
- [x] Independent code/security review completed; validated-input replacement race
      and upload inspection-error handling are resolved. Final focused tests:
      19 passed; test lint/format and diff checks passed.
- [ ] Positive API-30 artifact build/upload validation remains outstanding.

## State 2026-10-10: pipeline card layout cleanup

- [x] Group app identity, align the five actions in their own row, and place patch
      count and selection in a normal-flow footer; preserve existing workflows.
- [x] Browser regressions cover 320/390/1280px reflow, 44px action targets, keyboard
      selection, dialog actions and top-level tab visibility after a markup repair.
- [x] Full suite: 1686 passed, 1 xfailed. Final accessibility/static checks:
      135 passed; focused lint/format and diff checks passed.
- External Obtainium launch and production-service validation were not run.

## State 2026-10-10: per-source patch selection, UI/API step 3 complete

- [x] Grouped catalog remains opt-in; default endpoint and legacy list payload
      remain compatible. Mapping POST is replacement; omitted exclusions survive.
- [x] UI request-ownership guard and stale/deleted selection handling reviewed;
      independent API, security, code and UI reviews resolved.
- [x] Full suite: 1684 passed, 1 xfailed; focused lint, format and diff checks passed.
- [ ] Real morphe-cli, production service, and deployment E2E were not run.
- Existing Ruff baseline and mypy missing-stub/findings remain; do not treat them
  as introduced by this documentation update.

## State 2026-10-10: per-source patch selection, backend step 2

- [x] Shared resolution supports mapped selections and legacy flat lists without
      rewriting the saved legacy shape; mapped stale sources are retained/warned,
      and a mapped selection with no applicable source fails before browser work.
- [x] Restrict version-list reads to selected providers while retaining each
      selected source's full compatible-version set; unrelated unreadable lists
      do not block mapped apps. Relevant aliases of local source files are rejected.
- [x] Build hash/record and update polling cover source mappings; regressions include
      CLI selector grouping, exclusions, legacy persistence, stale APK protection,
      path aliases, and update detection.
- [x] Local full suite: 1648 passed, 1 xfailed. Focused Ruff/format/diff checks pass;
      backend Ruff and mypy retain findings documented with this step's report.
- [x] Independent code and security re-review completed; early-failure and source
      alias findings are resolved. No confirmed in-scope finding remains.
- [ ] Real morphe-cli, live service, and browser validation were not run. A valid
      mapped selection's full pipeline has not been exercised end to end.
- Step 3 is now complete; real-tool and deployment validation remains outstanding.

## State 2026-10-08: restrained glass restyle

- [x] Saved the resolved direction in `DESIGN.md`; retained Jinja, Alpine and
      Tailwind, with no Svelte or new dependencies.
- [x] Glass on the header and active dialog only; solid cards, blue accent,
      contrast-safe neutrals, reduced-motion and opaque blur fallback.
- [x] Browser regressions cover mobile header reflow and both dialogs' keyboard
      focus, Escape closure and opener restoration.
- [x] Final suite after mutation feedback: 1544 passed, 1 expected failure;
      independent review of the prior restyle found no regression.
- [x] Local mocked-API browser checks: no horizontal overflow at 320, 390, 768
      and 1280px; patch selection/save, upload request and Secrets error/retry.
- [ ] Live production API, browser zoom, reduced-motion and blur-fallback browser
      checks remain unverified. Screenshot read in the primary session was denied.
- [x] Upload HTTP errors and network failures stay in the dialog with inline
      feedback; browser retries preserve the selected file and version.
- [x] Remaining build, source, app, patch-save and notification actions check
      refusals and network failures before changing UI state; failures display inline.
- [x] Local mocked-browser mutation regressions cover refusals, network failures,
      malformed responses, retained form state, confirmation and successful actions.
- Changes remain uncommitted; earlier token and licence-key work is preserved.

## State (end of session 2026-10-05)

- Branch `secrets-guards-worker`, not merged into `main`, not pushed. `main` is
  at `482a3c9`.
- Gates on the branch: 1006 tests pass plus one expected failure (about 7 s);
  `ruff check src tests` 65; `mypy src tests` 10.
- Nothing on the branch has been run against a real browser, real `morphe-cli`,
  a live server, a reverse proxy or Docker. One exception: the patch-list repair
  was run against the real `SysAdminDoc/Hushfacebook` source in a scratch folder.
- Run the server: `uv run uvicorn src.main:app --host 0.0.0.0 --port 8000`.
  Set `DATA_DIR` to keep data out of the repo. A hostname other than `localhost`
  or an IP needs `ALLOWED_HOSTS`.
- First build after deploying: every app is rebuilt once and gets a new version
  string, so Obtainium offers one update per app.

## State 2026-10-06: local page assets, merged into `main`

- The admin page loads nothing from a third party: `src/static/app.css` is
  built with the Tailwind 3.4.17 standalone CLI (`uv run python
  scripts/build_css.py`) and committed; Alpine 3.17.4 is vendored.
- Gates on `main`: 1408 tests pass plus one expected failure;
  `ruff check src tests scripts` 60; `mypy src tests scripts` 10.
- Independently reviewed; the review findings are fixed. Merged into `main`
  (merge commit `8832363`), not pushed.
- [ ] Browser check that the page looks as before (same Tailwind version and a
      class-coverage test are the only evidence so far)
- [ ] The linux-arm64 and macOS Tailwind binaries are pinned by hash but were
      never downloaded or run
- [ ] The page uses absolute paths everywhere (`/static`, `/icons`,
      `/api/ui`), so it does not work under a reverse-proxy path prefix
- [x] `.claude/` gitignored

## State 2026-10-06: review and fix round done and committed

- Gates: 1293 tests pass plus one expected failure;
  `ruff check src tests` 60; `mypy src tests` 10.
- `secrets-guards-worker` is merged into `main`; `main` is not pushed.
- Still nothing run against a real browser, real
  `morphe-cli`, a live server, a reverse proxy or Docker.

Done 2026-10-06 (details in `decisions.md`):

- [x] Independent review of the unreviewed work: security, token and patch
      list, worker and guards. No critical security finding; one high regression
      in the patch-list sync
- [x] Lane 1, patch-list sync: staged pair, stricter patch-list check, repo list
      before unnamed assets, tags with `/`, byte limits
- [x] Lane 2, worker bookkeeping: build and sync claimed at request time,
      updates generation, unique temp files, `busy_since`
- [x] Lane 3, security lows: secret memory, no request input in errors,
      loopback port, `Sec-Fetch-Site`, server URL rules, token state
- [x] Review of the three lanes (security and code): two stuck-claim defects and
      several low findings
- [x] Follow-up round for those findings

Open after this round:

- [ ] The follow-up round has had no independent review (20 small fixes in
      `main.py`, `patcher.py`, `request_guards.py`, `secrets_store.py`); the
      owner decided 2026-10-06 not to review it
- [x] Merged into `main` 2026-10-06 (merge commit `253c462`); not pushed
- [x] Template: show `github_token_rejected`, `github_token_warning`,
      `busy_since`, "sync already running"; "starting" shows as amber
- [ ] `include_patches` and the app name are not validated and reach an error
      text that is returned masked (a guessed secret can still be confirmed there)
- [ ] App versions taken from a patch list enter file names unsanitised (not
      exploitable as written)
- [ ] `Sec-Fetch-Site` is not sent on plain `http://<LAN IP>`; cross-site GETs
      to `/api/ui/` are not refused there
- [ ] A cron build that arrives during a manual build is skipped until the next
      tick (now logged as a warning)
- [ ] Unverified: how GitHub writes a `/` and other special characters of a tag
      or asset name in `browser_download_url`
- [x] Backend Apprise URL secrets: bounded JSON-array environment/file
      precedence, explicit migration, write-only routes, safe local validation,
      stored-effective test notifications, and pinned cross-thread credential masks.
- [x] Settings UI for Apprise URLs: masked ordered drafts, count/source status,
      explicit migration, environment locks and inline failure feedback.
- [x] Apprise integration: 1622 tests passed, 1 expected failure; focused lint,
      formatting and diff checks passed. Full mypy retains 9 baseline findings.
      Independent security re-review found no new confirmed issue. Representative
      real Apprise workers were exercised with transport stubbed, including active
      rotations, credential-bearing exceptions and startup log masking.
- [ ] Real notification delivery and exhaustive transport-specific masking remain
      unverified; browser validation used local API fixtures only.
- [x] Settings says permissions cannot be verified for fine-grained tokens
- [ ] Not now: builds of pinned versions and local APKs without the browser

## Next session, in this order

- [x] The GitHub-token Settings slice shows the warning, rejection and
      permissions state, and handles token status without returning values
- [x] Apprise notification URL backend; GitHub token and cloakbrowser licence
      key fields remain in the existing Settings Secrets UI
- [ ] Try a real token: authenticated sync, the rate limit rising to 5,000, a
      wrong token giving the fixed 401 text on the Discovery tab
- [x] Secrets step 4: Settings controls for GitHub token, cloakbrowser licence
      key and write-only Apprise destinations are implemented and tested.
- [x] Secrets step 5 code: app-owned isolated cloakbrowser cache (refuses an
      existing `license.key` without reading it; fresh binary may be downloaded);
      explicit effective key; code-77 fallback; late denial closes browser and
      fails current task; later tasks remain keyless until rotation. Regression
      coverage includes release/variant/download selectors, static fallback,
      metadata extraction, build task propagation/slot cleanup, refresh-task
      degradation after worker cleanup, failed keyless relaunch and isolated test
      cache. Licence-key Settings field is now wired to the existing secret routes;
      real browser validation remains outstanding.
- [ ] Try with a real licence key and browser (not used or launched here); repeat
      an end-to-end validation after production cache refresh.
- [x] Independent static security review of the late-denial fixes completed;
      regression corrections cover the additional audited catch boundaries.
- [x] Full pytest passed: 1528 passed, 1 xfailed. Ruff focused checks and
      formatting passed. Full `mypy src tests scripts` now has 9 unrelated
      baseline errors; no errors remain in `tests/test_cloakbrowser_license.py`.
- [x] Secrets step 6 docs: README and architecture cover environment/file
      precedence, runtime variables, access-control/TLS limits, host/origin rules,
      proxy Host handling, token least privilege and cloakbrowser cache/worker
      recovery. Root/backend codemaps document secret/request-guard modules and
      runtime paths. Links, Docker example syntax and claims were checked against
      the current implementation; no source or deployment files changed.
- [x] Secrets step 7: icon lookup/download failures no longer discard extracted
      APK metadata; upload filename still uses the submitted version
- [x] Per-source patch selection step 2: backend reads both selection shapes;
      bare names resolved at build time; a source is a provider only when its own
      patches are selected. Narrow "one unreadable list blocks every app" here
- [ ] Per-source patch selection step 3: API and dialog show same-named patches
      per source, tabs keyed by repo, the mapping is saved and validated

## To verify with real tools (nothing here is done)

- [ ] Start the server from the branch; Java and Scraper pills; browser launches
      on cloakbrowser 0.5.12
- [ ] Sync Sources repairs the Hushfacebook list in the real data folder
- [ ] Build Facebook with the Hushfacebook patches and real `morphe-cli`
- [ ] A multi-source build applies the selection of every bundle
- [ ] Browser check of the UI: add-source errors, "Sync failed" notice, card
      error line, Save Settings error, the Scraper pill states, both dialogs
- [ ] Behind a reverse proxy: writes work with the full origin in `ALLOWED_HOSTS`
- [ ] Docker end-to-end test (plan step 7; no container runtime on this machine)

## Open, not scheduled

- [ ] A browser task that hangs forever blocks the worker (no watchdog)
- [ ] A failed in-task browser relaunch does not trigger worker recovery
- [ ] Buttons other than Save Settings ignore a refused write
- [ ] A scrape that fails is retried on every updates refresh
- [ ] A source without releases reports "updated" on every sync
- [ ] Download progress bar shows more than 100% for compressed responses
- [ ] Redaction matches exact values only (encoded, truncated or split forms
      pass); prints outside `patcher.py` and outside a build are not redacted
- [ ] A patch bundle can read `DATA_DIR/.env` as the same user (accepted)
- [ ] A valid patch list over 256 KiB under an unusual asset name, with no list
      in the repo, is not found
- [ ] cloakbrowser licence key format is unknown; the store's value pattern may
      refuse it
- [ ] Container page crash: cause not found (memory limit, `docker stats`)

## Done 2026-10-05 (details in `plan.md` and `decisions.md`)

- [x] Step 4 leftovers: `is_base` flag, `add_app` validation, unreadable patch
      list handling, atomic downloads
- [x] Build fixes: selectors follow their bundle, one forced rebuild, empty
      builds fail, names match ignoring case
- [x] cloakbrowser 0.5.12, keyless; auto-update off in Docker
- [x] Secret store and routes; CORS removed
- [x] Request guards: Host allow-list, Origin check, no framing, sync as POST,
      `/api/ui/source_updates` deleted, one updates refresh at a time
- [x] Scraper worker recovery, browser closed on shutdown, updates route
      degrades, browser state in tools status
- [x] Tests cannot write to the real data folder, launch a browser or open a
      network connection
- [x] Before the token: repo name rule, environment secrets validated and
      removed from the process environment, global redaction, Java processes
      without the secrets, bad and colliding sources reported
- [x] Wrong `.json` release asset taken as the patch list (Hushfacebook)
- [x] Secrets step 3: GitHub token sent to `api.github.com` only, header only;
      401 reported, no retry; rate-limit hint; scope warning for classic tokens;
      tag and asset URLs validated (not reviewed; no real token used; a first
      sync of both real sources without a token was run in a scratch folder)

## Done: rest of step 4

- [x] Upload path validated
- [x] Version choice is the same on every run; base source recognised by repo
- [x] App names with an apostrophe no longer break the pipeline card
- [x] Cron schedule follows the saved settings under concurrent saves
- [x] `/download/<package id>` matches that package only
- [x] `/api/apps/{id}` answers 404 for an unknown app; missing `obtainium_id` handled

## Done: step 4, Obtainium-facing build fixes

- [x] Changing an app's patch selection triggers a rebuild
- [x] Version string and filename change with the used sources and the selection
- [x] A failed patch leaves the previous APK served
- [x] `/download` answers 404 when nothing matches
- [x] Tests can no longer touch the real data directory (`tests/conftest.py`)

## Done: config safety (plan step 3)

- [x] Atomic writes of `config.yaml`
- [x] Every read-modify-write serialised and applied to the current file contents
- [x] `run_pipeline` writes only the fields it owns; UI changes made during a build
      survive, and a deleted app is not brought back
- [x] No lock held across network calls or patching
- [x] Independent review; one stale-write path in `check_source_updates` fixed
- [x] 19 tests in `tests/test_config_safety.py`; 67 pass in total

## Done: GitHub rate-limit failures (rest of plan step 2)

- [x] `add_source` returns HTTP 429 naming the rate limit and reset time
- [x] Build log shows a rate-limit line; the build carries on with local files
- [x] "Sync Sources" failure shown on the Discovery tab via `tools_status.sync_error`
- [x] 12 tests in `tests/test_rate_limit.py`; 48 pass in total; ruff and mypy unchanged
      from baseline

## Done: retrofit scraper changes to the Python rules (plan step 1a)

- [x] Dev dependencies: pytest, ruff, mypy; `playwright` as a direct dependency
- [x] pytest tests for the scraper changes (recovery, version lookup, log routing,
      patcher fallback): 15 tests in `tests/test_scraper.py`
- [x] Type hints, docstrings, specific exceptions, 88-char lines
- [x] ruff and mypy clean on the changed code

## Done: add-source flow

- [x] Normalise and validate the repo input in `add_source` (`src/main.py:454`)
- [x] Return a clear error and show it in the UI (`index.html:289`)
- [x] Stay on the Discovery tab after adding a source
- [x] Use the `name` field from the patch JSON for app names (`src/patcher.py:952`)
- [x] Make "Sync Sources" force a check despite the 6-hour throttle
- [x] "No releases" fallback: kept; add_source now reports the missing .mpp or JSON

## Done

- [x] Code review of the whole app
- [x] Browser relaunch and retry on a dead page or browser
- [x] Scraper logs in the build console
- [x] Newest version read from the search results
- [x] Original search URL restored
