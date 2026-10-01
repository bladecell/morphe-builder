# Todo

Current working checklist. The full list lives in `plan.md`.

## State

- Scraper resilience changes are done and tested against live APKMirror.
- Add-source flow items are done and covered by `tests/test_sources.py` (uncommitted UI
  behaviour not yet checked in a browser).
- Not yet tested: a full build (download + patch) and anything inside Docker.

## Next up: retrofit scraper changes to the Python rules (plan step 1a)

- [ ] Dev dependencies: pytest, ruff, mypy
- [ ] pytest tests for the scraper changes (recovery, version lookup, log routing,
      patcher fallback)
- [ ] Type hints, docstrings, specific exceptions, 88-char lines
- [ ] ruff and mypy clean on the changed code

## Then: add-source flow

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
