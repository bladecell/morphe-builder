# Todo

Current working checklist. The full list lives in `plan.md`.

## State

- Scraper resilience changes are done and tested against live APKMirror.
- Everything is uncommitted: `src/apkmirror_downloader.py`, `src/patcher.py`,
  plus the earlier hot-patches in `src/main.py` and `src/patcher.py`.
- Not yet tested: a full build (download + patch) and anything inside Docker.

## Next up: retrofit scraper changes to the Python rules (plan step 1a)

- [ ] Dev dependencies: pytest, ruff, mypy
- [ ] pytest tests for the scraper changes (recovery, version lookup, log routing,
      patcher fallback)
- [ ] Type hints, docstrings, specific exceptions, 88-char lines
- [ ] ruff and mypy clean on the changed code

## Then: add-source flow

- [ ] Normalise and validate the repo input in `add_source` (`src/main.py:454`)
- [ ] Return a clear error and show it in the UI (`index.html:289`)
- [ ] Stay on the Discovery tab after adding a source
- [ ] Use the `name` field from the patch JSON for app names (`src/patcher.py:952`)
- [ ] Make "Sync Sources" force a check despite the 6-hour throttle
- [ ] Decide what to do with the JSON-only "no releases" fallback

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
