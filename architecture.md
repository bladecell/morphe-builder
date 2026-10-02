# Architecture

High-level design of Morphe Builder as it stands.

## What it does

A self-hosted web app that downloads Android APKs from APKMirror, patches them with
Morphe patch bundles, and serves the result to Obtainium.

## Components

| Part | File | Role |
|---|---|---|
| Web app and API | `src/main.py` | FastAPI app, UI routes, Obtainium routes, scheduling, build orchestration |
| Config store | `src/config_store.py` | All `config.yaml` I/O: lock, atomic write, `update_config` |
| Pipeline | `src/patcher.py` | Tool and source sync, version selection, APK download, patching |
| Scraper | `src/apkmirror_downloader.py` | Drives a stealth Chromium (cloakbrowser) through APKMirror |
| UI | `src/templates/index.html` | Single page, Alpine.js + Tailwind from CDN, three tabs |
| Obtainium page | `src/templates/obtainium.html` | Minimal HTML page Obtainium parses for version and link |

External tools, downloaded at runtime: `morphe-cli.jar` (patcher) and `APKEditor.jar`
(merges split bundles, reads APK info). Both need Java 17.

## Data directory

Everything persistent lives under `DATA_DIR` (`/data` in Docker, project root otherwise).

| Path | Contents |
|---|---|
| `config.yaml` | Sources, apps, settings, last successful build per app |
| `bin/` | Tool jars, per-source `.mpp` and `.json`, `versions.json`, `version_cache.json`, `metadata.json`, `build.log` |
| `apks/raw/` | Downloaded original APKs, `<package>-<version>.apk` |
| `apks/patched/` | Patched APKs, `<package>-<version>-<patch tag>-<hash>.apk` |
| `apks/.building/` | Scratch output of the patch in progress; never served |
| `apks/icons/` | App icons |

## Main flows

**Add a source.** The UI posts a GitHub repo. The backend fetches the latest release,
downloads the `.mpp` bundle, and fetches `patches-list.json` from the repo at the
release tag. Discovery lists every package named in the active sources' JSON files.

**Configure an app.** The UI loads the patches compatible with a package from all
active sources and saves the selected patch names into `config.yaml`.

**Build.** `run_pipeline` syncs tools and sources, then for each app:

1. Pick a version: pinned in config, else newest recommended by the patch sources,
   else newest release on APKMirror.
2. Download the APK via the scraper, merging split bundles with APKEditor.
3. Skip the app if its recorded build used the same app version, patch selection and
   source versions and the APK still exists.
4. Run morphe-cli with the selected patches and the `.mpp` bundles that provide them,
   writing to `apks/.building/`. Only a successful result is moved into
   `apks/patched/`; a failed build leaves the previous APK served.
5. Record the result in `config.yaml`. The version string Obtainium sees is
   `<app version>-<patch tag>-<hash>`; the hash changes with the used source
   versions and the patch selection.

**Serve.** `/view/<id>` and `/api/apps/<id>` give Obtainium the version and a
`/download/<file>` link.

## Concurrency

- **Scraper worker.** One daemon thread owns the single browser instance. All scraping
  and all builds are queued onto it, so only one runs at a time.
- **Scheduler.** APScheduler runs the cron build, an hourly version refresh, and
  manually triggered builds.
- **Build log.** During a build `sys.stdout` is swapped for a tee that also writes
  `bin/build.log`; the UI polls that file.
- **Config.** Every change to `config.yaml` goes through
  `config_store.update_config`, which loads the current file, applies a small change
  and replaces the file atomically under one in-process lock. The pipeline writes only
  the fields it owns (per-app status and last build, source and tool versions), so UI
  changes made during a build survive. The lock is per process: one uvicorn worker
  only.

## Scraper

Flow for a download: search page -> release page -> variant page -> download page ->
tokenised file link. Variants are scored for arm64, nodpi and bundle preference.

Version lookups only load the search page. Navigation goes through `_goto`, which
relaunches the browser and retries once if the page or browser has died.

## Deployment

Docker image on `python:3.12-slim` with Java 17, dependencies installed by `uv`.
Runs as root with Chromium unsandboxed. No authentication on any endpoint.
