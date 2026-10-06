# Todo

Current working checklist. The full list lives in `plan.md`.

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

## State 2026-10-06: review and fix round done and committed

- Gates: 1293 tests pass plus one expected failure;
  `ruff check src tests` 60; `mypy src tests` 10.
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
- [ ] Merge into `main` (owner decided: merge after the fix round)
- [ ] Template: show `github_token_rejected`, `github_token_warning`,
      `busy_since`, "sync already running"; "starting" shows as a red dot
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
- [ ] With the Settings UI (step 4): Apprise notification URLs as secrets;
      "permissions cannot be verified" for a fine-grained token
- [ ] Not now: builds of pinned versions and local APKs without the browser

## Next session, in this order

- [ ] The token-permissions warning is in `tools_status` only
      (`github_token_warning`); the UI does not show it yet (do with step 4)
- [ ] Try a real token: authenticated sync, the rate limit rising to 5,000, a
      wrong token giving the fixed 401 text on the Discovery tab
- [ ] Before the Settings UI: pin Tailwind and Alpine with integrity hashes or
      vendor them. They load from a CDN with a floating version and would see a
      secret as it is typed
- [ ] Secrets step 4: Settings UI for both secrets. Must show the
      `environment_invalid` state and the token-permissions warning
- [ ] Secrets step 5: licence key passed to the browser explicitly
      (`launch(license_key=...)` only when set); keyless fallback when the key is
      rejected; relaunch at a task boundary on a key change. Until this lands a
      key from the environment does not reach the browser. Then a security review
- [ ] Secrets step 6: docs. README: `ALLOWED_HOSTS` (bare name = Host only, full
      origin = Origin too), the proxy must pass `Host` through, both secret
      variables, a token with no permissions. `src/codemap.md` and
      `src/templates/codemap.md` still name the deleted `source_updates` route.
      `architecture.md`: secret store, request guards, worker states
- [ ] Secrets step 7: upload keeps the metadata when the icon is missing
- [ ] Per-source patch selection step 2: backend reads both selection shapes;
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
