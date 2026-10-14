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
| Secret store | `src/secrets_store.py` | GitHub secret plus write-only Apprise URLs: environment/file precedence, validation, redaction and atomic file updates |
| Request guards | `src/request_guards.py` | Host, Origin and Fetch Metadata checks for the unauthenticated app |
| Pipeline | `src/patcher.py` | Tool and source sync, version selection, APK download, patching |
| Scraper | `src/apkmirror_downloader.py` | Drives headless ShardX Chromium (Playwright over CDP) through APKMirror; resolves variant pages via the Morphe API |
| UI | `src/templates/index.html` | Single page, three tabs; vendored Alpine.js and a prebuilt Tailwind stylesheet from `src/static/` |
| Obtainium page | `src/templates/obtainium.html` | Minimal HTML page Obtainium parses for version and link |

External tools, downloaded at runtime: `morphe-cli.jar` (patcher) and `APKEditor.jar`
(merges split bundles, reads APK info). Both need Java 21.

## Data directory

Everything persistent lives under `DATA_DIR` (`/data` in Docker, project root otherwise).

| Path | Contents |
|---|---|
| `config.yaml` | Sources, apps, settings, last successful build per app |
| `.env` | Optional plaintext secrets file, mode `0600`; environment values override it |
| `bin/` | Tool jars, per-source `.mpp` and `.json`, `versions.json`, `version_cache.json`, `metadata.json`, `build.log` |
| `apks/raw/` | Downloaded original APKs, `<package>-<version>.apk` |
| `apks/patched/` | Patched APKs, `<package>-<version>-<patch tag>-<hash>.apk` |
| `apks/.building/` | Scratch output of the patch in progress; never served |
| `apks/icons/` | App icons |

## Main flows

**Add a source.** The UI posts a GitHub repo. The backend fetches the latest release,
downloads the `.mpp` bundle, and fetches `patches-list.json` from the repo at the
release tag. Discovery lists every package named in the active sources' JSON files.

**Configure an app.** The UI loads compatible patches from the opt-in grouped
catalog and saves repo-keyed selections into `config.yaml` only on explicit save.
The default catalog response and legacy flat selections remain supported; see
[README patch selection API](README.md#patch-selection-api).

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

## Secrets and request trust

`GITHUB_TOKEN` and `APPRISE_URLS` are stored by
`secrets_store`. Apprise URLs are a bounded JSON array, are write-only through
`PUT`/`DELETE /api/ui/secrets/apprise-urls`, and can be imported from the legacy
`config.yaml` setting only through `POST /api/ui/secrets/apprise-urls/migration`.
Each array item must parse to exactly one Apprise destination. Malformed existing
`APPRISE_URLS` file content is authoritative and causes a conflict rather than
being overwritten. Accepted URL credentials are masked using Apprise's private
field metadata. Active notifications pin masks until all notifier threads
complete; bounded admission never evicts active masks. Effective URLs are
registered during startup and secret-status resolution, without delivery.
Invalid/rejected URLs do not enter persistent redaction state.
Migration never overwrites a different file value; matching file storage allows
cleanup retry. Writes land before legacy cleanup, so stored values remain
authoritative if cleanup fails. The old config list remains a runtime fallback
until an explicit migration or mutation succeeds. With no environment value, the store reads/writes
`DATA_DIR/.env`; writes are atomic and mode `0600`. The file is plaintext and
must not be loaded as an environment file or sourced by a shell. At application
startup, `absorb_environment()` captures these variables in memory and removes
them from `os.environ`; a set environment value takes precedence over the file
and locks that secret against UI changes. An invalid environment value is
reported as invalid and does not fall back to the file. Secret status endpoints
return source/set state only, never values. Notification status additionally
reports count, legacy availability and migration state. Only GitHub-token changes schedule a source sync.

The GitHub token is used as an authorization header only for `api.github.com`;
child processes receive an environment with the secrets removed. Patch bundles
execute in those child processes, so a token without permissions is
recommended. Scope reporting is available for classic tokens; fine-grained and
GitHub App token permissions may be unverifiable.

At startup `secrets_store.discard_legacy_license_key()` strips a stale
`CLOAKBROWSER_LICENSE_KEY` from `.env` and the environment, and
`main.remove_legacy_cloakbrowser_cache()` deletes `DATA_DIR/bin/cloakbrowser`.

The app has no authentication on its UI, APIs, or Obtainium routes. It has
Host/Origin/Fetch Metadata request guards, not an authentication boundary. Keep
it on a trusted network or enforce access control at the reverse proxy, and use
TLS for remote access. `ALLOWED_HOSTS` is a comma-separated list: a bare host
allows that Host name and adds no separate Origin trust entry (the normal
same-host/same-port Origin check still applies); a full origin (scheme, host,
and port) also allows that exact Origin. With a full-origin entry,
`settings.server_url` is not used as a trust anchor. Proxies must preserve the
original `Host`; the app ignores `X-Forwarded-Host`.

## Scraper

Flow for a download: search page -> release page -> variant page -> download page ->
tokenised file link. Variants are scored for arm64, nodpi and bundle preference.

Browser: `_launch` starts the ShardX engine headless through its SDK with `cdp=True`
and attaches sync Playwright via `connect_over_cdp`, using a throwaway profile in a
temp dir (`--no-sandbox` when root); `_teardown` stops Playwright, the engine and
removes the profile. The engine is fetched at Docker build time, never at runtime.
Source order in `patcher.download_apk`: APKMirror; then Uptodown, using the page the
Morphe API resolved (`ApkMirror_Downloader.download_from_uptodown`: exact page version
required, file host and type checked, package id re-read after merge); then APKPure via
`apkeep`. Uptodown and APKPure files are unsigned third-party copies.
Risk: the ~170 MB Chromium engine comes from a ProxyShard CDN and is unsigned;
see the Dockerfile for how the build verifies it.

Version lookups only load the search page. Navigation goes through `_goto`, which
relaunches the browser and retries once if the page or browser has died.

## Deployment

Docker image on `python:3.12-slim` with Java 21 (Temurin), dependencies installed by `uv`.
Runs as root with Chromium unsandboxed; ShardX engine and `apkeep` are fetched at build time. Runtime variables such as `GITHUB_TOKEN`,
JSON-array `APPRISE_URLS`, and `ALLOWED_HOSTS` can be passed with Docker `-e`
without rebuilding the image. See [README deployment guidance](README.md#runtime-environment)
and [host/proxy guidance](README.md#host-and-reverse-proxy-checks). No application
authentication is provided.
