# Repository Atlas: morphe-builder

## Project Responsibility

Self-hosted web app that downloads Android APKs from APKMirror, patches them with
Morphe patch bundles (`.mpp`) using `morphe-cli`, and serves the results to Obtainium.
The user adds patch sources (GitHub repos), picks an app and its patches, and builds on
demand or on a cron schedule.

For design rationale see [architecture.md](architecture.md) and
[decisions.md](decisions.md); for planned work see [plan.md](plan.md).

## System Entry Points

- `src/main.py`: FastAPI app `src.main:app`. Run with
  `uv run uvicorn src.main:app --host 0.0.0.0 --port 8000`.
- `src/patcher.py`: `python -m src.patcher` runs one full pipeline pass without the web
  app.
- `Dockerfile`: `python:3.12-slim-bookworm` plus Java 17, `uv sync --frozen`,
  Playwright Chromium and the cloakbrowser binary. Sets `DATA_DIR=/data`, exposes 8000.
- `pyproject.toml`: dependencies and tool config (pytest `pythonpath = ["."]`, ruff
  line length 88, mypy). Python >= 3.12, managed with `uv`.

## Directory Map (Aggregated)

| Directory | Responsibility Summary | Detailed Map |
|-----------|------------------------|--------------|
| `src/` | Backend: FastAPI routes and scheduling (`main.py`), locked atomic config I/O (`config_store.py`), sync, version selection, download and patch pipeline (`patcher.py`), APKMirror browser scraper (`apkmirror_downloader.py`). | [View Map](src/codemap.md) |
| `src/templates/` | Jinja2 views: Alpine.js single-page admin UI and the Obtainium HTML page. | [View Map](src/templates/codemap.md) |

Not mapped:

- `tests/`: pytest suite (`test_sources.py`, `test_scraper.py`, `fixtures/`).
- `test.py` (root): not a test. A standalone `rnet`-based APKMirror scraper that
  predates `src/apkmirror_downloader.py`. Nothing imports it and `rnet` is not a
  declared dependency.

## Architecture at a Glance

```
Browser UI (index.html, Alpine) ──/api/ui/*──┐
Obtainium ──/view, /api/apps, /download──────┤
                                             v
                                   src/main.py (FastAPI)
                                   ├─ APScheduler: cron build, hourly version refresh
                                   └─ ScraperWorker thread (owns the one browser)
                                             │ run_pipeline(scraper=...)
                                             v
                                   src/patcher.py
                                   ├─ GitHub releases -> bin/*.jar, *.mpp, *.json
                                   ├─ java -jar APKEditor.jar (merge, info)
                                   └─ java -jar morphe-cli.jar patch
                                             │
                                             v
                                   src/apkmirror_downloader.py -> APKMirror
```

Key constraints:

- One browser, one worker thread: all scraping and all builds are serialised through
  `ScraperWorker.execute`.
- `config.yaml` is the only database. All changes go through
  `config_store.update_config` (in-process lock, atomic replace), which requires a
  single uvicorn worker.
- No authentication on any endpoint.

## Runtime Data (`DATA_DIR`)

`DATA_DIR` is `/data` in Docker and the project root otherwise. All of it is
gitignored.

| Path | Contents |
|---|---|
| `config.yaml` | Sources, apps, settings, last successful build per app |
| `bin/` | Tool jars, per-source `.mpp` and `.json`, `versions.json`, `version_cache.json`, `metadata.json`, `build.log` |
| `apks/raw/`, `apks/patched/`, `apks/icons/` | Downloaded APKs, patched APKs, app icons |

File-level ownership of each of these is in [src/codemap.md](src/codemap.md).
