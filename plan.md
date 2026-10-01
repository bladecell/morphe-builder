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
- [ ] Verify a full build (download + patch) and a run inside Docker

## 1a. Bring the scraper changes up to the project Python rules

Decided 2026-10-01: retrofit first, then continue with step 2 under the same rules.

- [ ] Add pytest, ruff and mypy as dev dependencies in `pyproject.toml`
- [ ] Add a test output folder to `.gitignore` if tests write files
- [ ] pytest tests for `_goto` recovery (page crash, browser closed, unrelated error
      re-raised), with the browser mocked
- [ ] pytest tests for `latest_version` and `_extract_first_valid_row` against saved
      search-page HTML
- [ ] pytest test that scraper logs follow a swapped `sys.stdout`
- [ ] pytest test for the APKMirror fallback in `get_supported_versions`, scraper mocked
- [ ] Type hints and docstrings (args, returns, raises) on the new and changed functions
- [ ] Catch specific exceptions in the recovery path instead of `except Exception`
- [ ] Lines within 88 characters; ruff and mypy clean on the changed code

## 2. Add-source flow

All work from here on follows the project Python rules, with pytest tests.

- [ ] Keep the inline sync so the patch list exists before the page re-renders
- [x] Normalise and validate the repo input (`github.com/...`, `.git`, casing, extra path)
- [x] Show add-source errors in the UI instead of ignoring the response
- [x] Stay on the Discovery tab after adding a source
- [x] Show real app names in Discovery instead of package IDs (`src/patcher.py:952`)
- [x] Make "Sync Sources" bypass the 6-hour throttle
- [ ] Remove or fix the "no releases" fallback that downloads only the JSON
- [ ] Surface GitHub rate-limit failures

## 3. Config safety

- [ ] One locked, atomic load-modify-save for `config.yaml`
- [ ] Stop `run_pipeline` overwriting UI changes made during a build (`src/patcher.py:789`)

## 4. Other bugs from the review

- [ ] Patches hidden across sources: dedup is by name only (`src/main.py:355`)
- [ ] Obtainium misses patch updates: version string always uses the Morphe tag
      (`src/patcher.py:878`)
- [ ] A failed patch leaves no APK: old file is deleted first (`src/patcher.py:892`)
- [ ] `/download/...` returns `null` with 200 instead of 404 (`src/main.py:810`)
- [ ] Unsafe upload path, no auth, CORS `*` (`src/main.py:525`)
- [ ] Version choice varies between runs: source special-cased by display name,
      set-order intersection (`src/patcher.py:359`)
- [ ] App names with an apostrophe break the pipeline card (`index.html:756`)

## 5. Cleanup (only if asked)

- [ ] `test.py` is the old scraper and imports an uninstalled package
- [ ] `/api/ui/source_updates` is never called by the frontend
- [ ] Dockerfile installs a second Chromium that is not used
