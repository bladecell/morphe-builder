# Todo

Current working checklist. The full list lives in `plan.md`.

## State

- `main` holds steps 1a, 2 and 3 and ten step 4 fixes, all committed (not pushed).
  129 tests pass.
- Not yet checked in a browser: the add-source UI behaviour, the red "Sync failed"
  notice and the pipeline card after the apostrophe fix.
- Not yet tested: a full build with real `morphe-cli`, a live server, Docker (step 7).
- First build after deploying: every app is rebuilt once and gets a new version string,
  so Obtainium offers one update per app.

## Waiting on a decision

- [ ] Authentication and CORS: which mechanism, and do the Obtainium endpoints stay
      open?
- [ ] Same-named patches in two sources: store the source with each selected patch?

## Next up

- [ ] Step 4 leftovers: base source by display name in the frontend, package id
      validation in `add_app`, silent drop of a corrupt patch JSON
- [ ] Step 7: Docker end-to-end test (blocked: no container runtime on this machine)
- [ ] Step 5: GitHub API token (where the token is stored gets decided when this
      step starts)

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

## Waiting on a decision

- [ ] Bump cloakbrowser to 0.5.x?
- [ ] Fix the scraper worker staying dead after a failed launch at startup?
- [ ] Container memory limit and `docker stats` output, to pin down the crash cause

## Done

- [x] Code review of the whole app
- [x] Browser relaunch and retry on a dead page or browser
- [x] Scraper logs in the build console
- [x] Newest version read from the search results
- [x] Original search URL restored
