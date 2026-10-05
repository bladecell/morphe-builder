# Todo

Current working checklist. The full list lives in `plan.md`.

## State

- `main` holds steps 1a, 2 and 3 and ten step 4 fixes, all committed (not pushed).
  Everything since (step 4 leftovers, build fixes, cloakbrowser bump, secret
  store, request guards) is in the working tree, uncommitted. 709 tests pass.
- Not yet checked in a browser: the add-source UI behaviour, the red "Sync failed"
  notice and the pipeline card after the apostrophe fix.
- Not yet tested: a full build with real `morphe-cli`, a live server, Docker (step 7).
- First build after deploying: every app is rebuilt once and gets a new version string,
  so Obtainium offers one update per app.

## Decided 2026-10-05

- [x] Authentication: none, the whole app stays open
- [x] Same-named patches in two sources: store the source with each selected patch
- [x] GitHub token: a Settings field that writes to `.env`
- [x] Bump cloakbrowser to 0.5.x
- [x] Docker end-to-end test: later

## Done 2026-10-05: step 4 leftovers (uncommitted)

- [x] `is_base` flag from the backend; the dialog no longer compares the name
- [x] `add_app` answers 400 for an invalid package id; the UI shows the error
- [x] Unreadable patch list: error on the app's card, 502 in the dialog, atomic
      downloads
- [x] Independent review, one blocking bug fixed (non-text patch name), re-reviewed
- [x] 190 tests pass; ruff 84 (was 88) and mypy 10 (unchanged) on `src tests`
- [ ] Browser check: base-source defaults with a renamed Morphe source, the alert on
      an invalid id, both dialogs on an error, the error line on the card

## Approved 2026-10-05: per-source patch selection (after the leftovers)

- [x] Checked with the released jar (v1.18.1, real runs): `-e` and `-d` apply to
      the bundle they follow. Today's command binds them all to the last bundle,
      so multi-source builds miss the patches of earlier bundles, exit 0
- [x] Step 1: build the command with each bundle followed by its own selectors
      (uncommitted; reviewed, verdict fit; 205 tests pass; not run with the real
      CLI). Affected apps are not rebuilt automatically
- [x] Step 1b (decided 2026-10-05): forced one-off rebuild through the version
      hash; fail a build with no applied patch or an unknown name; match names
      ignoring case (uncommitted; reviewed, review fixes applied and checked by
      breaking the code in a scratch copy, not re-reviewed; not run with the
      real CLI)
- [ ] Step 2: backend reads both selection shapes; bare names resolved at build
      time; a source is a provider only when its own patches are selected
- [ ] Step 3: API and dialog show same-named patches per source, tabs keyed by
      repo, the mapping is saved and validated

## Approved 2026-10-05: secrets, GitHub token, cloakbrowser (one step per round)

- [x] Step 0: bump cloakbrowser to 0.5.12, keyless; auto-update off in Docker
      (uncommitted; 200 tests pass; no browser launched, no Docker build)
- [x] Step 1: secret store and routes; `.env` ignored; CORS same-origin
      (uncommitted; security review found no leak path)
- [x] Step 1b (decided 2026-10-05): refuse state-changing requests from another
      origin; discovery sync as POST; Host allow-list with `ALLOWED_HOSTS`
      (uncommitted; 593 tests pass; security review: fit after three fixes)
- [x] Step 1c (decided 2026-10-05): `ALLOWED_HOSTS` names accepted as Origin;
      no framing; loopback server URL trusted only for a loopback Host; Save
      Settings shows errors (uncommitted; 644 tests pass; not yet re-reviewed;
      not run with a browser or a proxy)
- [x] Step 1d: one refresh at a time on `/api/ui/updates` and
      `/api/ui/source_updates`; no config write in `source_updates`
      (uncommitted; 654 tests pass; security review: fit after two decisions)
- [x] Step 1e (decided 2026-10-05): full origins in `ALLOWED_HOSTS`; delete
      `/api/ui/source_updates`; cache stamp at completion; framing headers on
      500s; proxy rule pinned by a test (uncommitted; 677 tests pass; not
      re-reviewed; not run with a browser or a proxy)
- [ ] Step 6 also: `src/codemap.md` and `src/templates/codemap.md` still name
      the deleted `source_updates` route
- [ ] Later: buttons other than Save Settings ignore a refused write; a failed
      scrape is retried on every refresh
- [ ] Before step 3: `REPO_PATTERN`, validate secrets from the environment,
      redact exception text, Java process without the secrets in its environment
- [ ] Before step 4: pin Tailwind and Alpine with integrity hashes or vendor them
- [x] Step 2: scraper worker recovers from a failed launch; browser closed on
      shutdown; updates route degrades; browser state in tools status
      (uncommitted; code review fixes applied, not re-reviewed; 709 tests pass;
      tests can no longer launch a browser or open a network connection; not
      run with a real browser or server)
- [ ] Open: a browser task that hangs forever blocks the worker (watchdog not
      built); a failed in-task relaunch does not trigger worker recovery
- [ ] Step 3: GitHub token on `api.github.com` calls; 401 reported
- [ ] Step 4: Settings UI for both secrets
- [ ] Step 5: licence key passed to the browser; keyless fallback; relaunch on
      a key change
- [ ] Step 6: docs
- [ ] Step 7: upload keeps the metadata when the icon is missing

## Waiting on a decision

- [x] cloakbrowser (decided 2026-10-05): bump to 0.5.x, keyless by default; use a
      free licence key when one is set in Settings. Design under investigation
      together with the GitHub token, since both are secrets set in the UI
- [x] A sync re-downloads a patch list that fails to load (done 2026-10-05,
      uncommitted; reviewed twice, verdict fit; 198 tests pass)
- [x] The pipeline card shows the error text of any app in "Error" status (decided
      2026-10-05: keep)
- [ ] Keep one unreadable list blocking every unpinned app until the selection
      names its source? (current behaviour, pinned by a test)
- [ ] Commit the step 4 leftovers?

## Next up

- [ ] Step 5: GitHub API token
- [ ] Step 7: Docker end-to-end test (deferred; no container runtime on this machine)

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
