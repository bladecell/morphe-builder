# src/

## Responsibility

The backend: the pipeline/scraper layers, persistence, request/secret controls,
and Jinja templates:

| Module | Layer | Role |
|---|---|---|
| `main.py` | Presentation + orchestration | FastAPI app, UI JSON API, Obtainium endpoints, APScheduler jobs, `ScraperWorker`, build task |
| `patcher.py` | Pipeline / service | GitHub release sync for tools and patch sources, version selection, APK download and split merge, morphe-cli invocation, metadata |
| `config_store.py` | Persistence | All `config.yaml` I/O: `load_config`, `update_config`, atomic write, `DATA_DIR`, `DEFAULT_CONFIG` |
| `secrets_store.py` | Secret persistence | Environment/file precedence for GitHub and Apprise secrets, plus `discard_legacy_license_key`; atomic `DATA_DIR/.env`, redaction, child-environment stripping |
| `request_guards.py` | Request security | Host, Origin, and Fetch Metadata checks; no authentication |
| `apkmirror_downloader.py` | Gateway | `ApkMirror_Downloader`: drives one headless ShardX engine (sync Playwright over CDP) page through APKMirror and Uptodown; Morphe API variant resolver; `search()` rejects pages whose package or version differs from the request (`_accept_if_requested`) |
| `templates/` | View | See [templates/codemap.md](templates/codemap.md) |

## Design

**Dependency direction.** `main -> patcher -> apkmirror_downloader`, with `main` and
`patcher` both depending on `config_store`, which depends on nothing in `src/`. `main`
gets `ApkMirror_Downloader` and `DATA_DIR` through `src.patcher`, not directly.

**HushFacebook artifact floor.** `patcher.run_pipeline` snapshots constrained raw
APKs into `.building` before APKEditor `minSdkVersion` inspection and patches that
same snapshot. `main.upload_apk` rechecks the selected-provider requirement under
the config-store lock immediately before atomic promotion; APK inspection stays
outside the lock.

**Config writes (unit of work).** `config_store.update_config(mutate)` holds one
process-wide `RLock` while it loads the current file, calls `mutate(config)` and, if
anything changed, replaces the file atomically (temp file in the same directory, fsync,
`os.replace`). If `mutate` raises, nothing is saved. Rules: never do slow work inside a
mutation, never nest `update_config`, and never write a snapshot loaded earlier.
`load_config` takes no lock and returns a private copy. The pipeline writes through
`patcher.save_app_result` (per-app `status`, `error`, `last_successful_build`; no-op if
the app was deleted) and `patcher._apply_versions` (source and tool versions). The lock
is per process, so the app must run as a single uvicorn worker.

**Single-owner browser (actor).** `ScraperWorker` in `main.py` is a daemon thread that
owns the only `ApkMirror_Downloader`. Work is submitted with
`SCRAPER_WORKER.execute(func, *args, **kwargs)`, which enqueues
`(func, args, kwargs, result_queue)`, blocks the caller until done, and re-raises any
exception. The worker calls `func(*args, scraper=self.downloader, **kwargs)`, so every
submitted callable must accept a `scraper` keyword. This serialises all scraping and
all builds. The thread clears its asyncio event loop because Playwright's sync API
requires none.

**Optional injected scraper.** `get_supported_versions`, `extract_app_metadata`,
`download_apk` and `run_pipeline` take `scraper=None`. When `None` they construct their
own `ApkMirror_Downloader` and close it in `finally`; when injected they leave it open.

**Module-level state in `main.py`.** `BUILD_STATUS`, `SCRAPER_ACTIVE`, `TOOLS_UPDATING`,
`SYNC_ERROR`, `DIAGNOSTICS`, `UPDATE_CACHE` (60 s TTL), `SOURCE_UPDATE_CACHE` (600 s TTL). Plain
dicts and booleans mutated from request threads, scheduler threads and the worker, with
no locks. Only `config.yaml` is protected.

**File-backed state under `DATA_DIR`** (env var, defaults to the project root):

| File | Written by | Contents |
|---|---|---|
| `config.yaml` | `config_store.update_config` | `repositories`, `sources`, `tools`, `apps`, `settings`. Shape in `config_store.DEFAULT_CONFIG`. Seeded from the project root copy or the defaults on first load |
| `.env` | `secrets_store` | Optional plaintext GitHub secrets and JSON-array `APPRISE_URLS`; atomic mode `0600`; never source/load as an environment file |
| `bin/versions.json` | `patcher.save_local_versions` | Release tag per GitHub repo, plus `<repo>#last_check` timestamps |
| `bin/version_cache.json` | `patcher.save_version_cache` | Newest APKMirror version per package, 12 h TTL |
| `bin/metadata.json` | `patcher.extract_app_metadata`, `patcher.save_metadata` | `real_name` and `version` per package |
| `bin/patches-<owner>-<repo>.{mpp,json}` | `patcher.check_and_download_release` | Patch bundle and patch list per source; paths from `get_source_paths` |
| `bin/build.log` | `main.build_task` | Captured stdout of the current or last build |
| `apks/raw/<pkg>-<version>.apk` | `download_apk`, `/api/ui/upload_apk` | Unpatched APKs |
| `apks/patched/<pkg>-<version>-<patch tag>-<hash>.apk` | `run_pipeline` | Output served to Obtainium; moved in atomically from `apks/.building/` |
| `apks/.building/` | `run_pipeline` | Scratch output of the patch in progress; wiped at the start of every build |
| `apks/icons/<pkg>.png` | scraper `download_file` | Mounted at `/icons` |

**Patch list schema tolerance.** Four functions parse the per-source patch JSON and all
accept the same variants: top level either a list or `{"patches": [...]}`,
`compatiblePackages` either a dict keyed by package or a list of objects with
`packageName` or `name`, and versions under `versions` or `targets[].version` (skipping
`isExperimental`). They are `main.get_patches_for_app`,
`patcher.get_supported_versions`, `patcher.patch_app` and
`patcher.get_all_available_apps`. A schema change touches all four.

**Build log capture.** `build_task` replaces `sys.stdout` with `Tee(stdout, log_file)`
for the duration of a build. Pipeline code uses `print`. The scraper's logger has a
`_StdoutHandler` that prints to whatever `sys.stdout` currently is, so its lines land
in `bin/build.log` too. `download_with_progress` writes `\r` progress bars; the log is
opened with `newline=""` so the UI can collapse them.

## Flow

### Startup (`main.startup_event`)

1. `secrets_store.absorb_environment` captures recognized environment secrets
   and removes them from `os.environ`; then prepare data directories and the
   delete the retired `DATA_DIR/bin/cloakbrowser` cache.
2. Load config, schedule jobs, and fill `DIAGNOSTICS` (Java on PATH, scraper
   importable).
3. `SCRAPER_WORKER.start()` launches the browser with the effective configured
   key passed explicitly, if present.
4. If either tool jar is missing, `update_tools_task` runs synchronously.
5. Start the scheduler and queue a one-off `refresh_versions_task` job.

### Scheduler jobs

`setup_cron_job` removes every job and, if `settings.cron_schedule` is set, registers
`periodic_build` (`build_task(is_cron=True)`) and an hourly `version_refresh`
(`refresh_versions_task`). It reruns on `/api/ui/save_settings`. Manual builds are
added as one-off scheduler jobs by `/api/ui/trigger_build`, not as FastAPI background
tasks, to get a thread without an event loop.

### Build (`build_task` -> `run_pipeline`)

1. `build_task` returns early if a build is running, or if a cron build already ran
   this minute. Sets `SCRAPER_ACTIVE`, submits `_do_build` to the worker.
2. `_do_build` sets `BUILD_STATUS`, installs the `Tee`, calls
   `run_pipeline(package_ids, quiet=is_cron, scraper=scraper)`.
3. `run_pipeline`: `load_config` -> `update_tools` -> `get_patch_versions`, then per
   app (5 s pause between apps):
   1. `get_supported_versions(force_refresh=True)`.
   2. Reuse `apks/raw/<pkg>-<v>.apk` if present, else `download_apk`.
   3. Skip if `is_build_current`: tools were not updated, the recorded APK exists, and
      the recorded app version, `patches` (see `selected_patches`) and versions of the
      sources used last time all still match.
   4. `patch_app` into `apks/.building/<tmp>/<pkg>.apk`. On success name the result
      with `build_combined_version` (`<app version>-<primary tag>-<8 hex>`, the hash
      covering the used source versions and the selection), `os.replace` it into
      `apks/patched/`, record it, then delete the app's older APKs. On failure
      `apks/patched/` is untouched and the previous APK stays served.
   5. Write `last_successful_build`, `status` and `error` with `save_app_result`. The
      pipeline keeps working from the snapshot it loaded at the start, so an app edited
      mid-build is built with its old patch selection; the recorded selection then
      differs from the config and the next build rebuilds it.
4. Back in `_do_build`: send an Apprise notification, restore stdout, zero
   `UPDATE_CACHE["timestamp"]`.

### Version selection (`get_supported_versions`)

Returns `(versions, None, None)`. Priority:

1. Pinned `version` on the app config. No network.
2. Newest version recommended by patch sources. Only sources that provide a selected
   patch, plus the base source (`is_base_repo`: `MorpheApp/morphe-patches`), are
   considered, in config order; their version sets are intersected, falling back to
   the non-base provider if the intersection is empty.
   With no patches selected, all sources are merged.
3. `scraper.latest_version`, cached in `bin/version_cache.json` for 12 h unless
   `force_refresh`. On scraper failure the stale cached value is returned.

### Tool and source sync (`update_tools` -> `check_and_download_release`)

Per repo: skip if checked within `UPDATE_CHECK_INTERVAL` (6 h) and files exist, unless
`force`. Otherwise query `api.github.com/repos/<repo>/releases/latest`, download assets
matching each wanted extension, and for `.json` fall back to
`raw.githubusercontent.com/<repo>/<tag>/` with `JSON_FILENAME_CANDIDATES`. A repo with
no releases falls back to `main` then `master` for the JSON only. The tag is recorded
only when every wanted file was obtained. Returns `True` only when something was
downloaded, so `False` means failure or already up to date.

A GitHub rate limit (403 or 429 with `retry-after` or `x-ratelimit-remaining: 0`)
raises `GitHubRateLimitError`, whose message carries the reset time. `update_tools`
stops at the first one, saves what it already synced and re-raises. Each caller handles
it: `add_source` returns HTTP 429, `run_pipeline` prints it and builds with the files on
disk, `update_tools_task` stores it in `SYNC_ERROR`, which `/api/ui/tools_status`
returns as `sync_error`.

### Download (`download_apk`)

`get_app_preferences` reads `apkFileType` from the patch lists to choose bundle or
standalone (default bundle) -> `scraper.search` -> `scraper.download_file` into a temp
dir (if APKMirror yields nothing: `scraper.download_from_uptodown`, then `apkeep`;
an Uptodown file must contain the requested package id) -> icon download -> if the file is a zip containing `.apk` entries, merge with
`java -jar APKEditor.jar m` -> `extract_app_metadata` (`APKEditor.jar info`).

### Patch (`patch_app`)

Reads the real package id via `APKEditor.jar info`, assigns each selected patch to the
first active source that provides it for that package, and falls back to the
`MorpheApp/morphe-patches` bundle if any patch is unassigned. Runs
`java -jar morphe-cli.jar patch --continue-on-error --exclusive <in> -o <out>
--patches <mpp>... -e <patch>... -d <patch>...`, streaming output. Any line containing
`SEVERE:` or a non-zero exit deletes the output and returns `(False, [])`; success
returns `(True, [repos used])`.

### Scraper (`ApkMirror_Downloader`)

- `search(package_name, version, ...)` -> `AppMetadata | None`: search URL from
  `_build_search_url` -> `_extract_first_valid_row` (skips alpha, beta, dev, canary
  when no version is given) -> release page -> `_get_best_variant` -> variant page ->
  `_extract_final_metadata`. Handles landing directly on a variants table or a download
  page. Returns `None` on any miss.
- `_get_best_variant` scoring: arch match +10 or universal +5, dpi match +5 or nodpi
  +3, bundle preference match +2. Defaults are `Arch.ARM64_V8A`, `Dpi.NODPI`.
- `latest_version(package_name)`: search page only, reads `Version:` from the
  `.infoSlide` following the first valid row. Raises on navigation failure or timeout.
- `download_file(target_url, dest_directory, filename)`: URLs containing `/download/`
  go through `_download_binary_via_browser` (download page -> `a#download-link` ->
  `page.expect_download`). Anything else (icons) uses `urllib`, falling back to the
  browser path.
- `_goto` is the only navigation entry point apart from the final download hop. It
  relaunches the browser and retries once when the Playwright error mentions `crash`
  or `closed`; timeouts are re-raised.
- `_launch` starts ShardX headless (`cdp=True`) in a throwaway temp profile, adds
  `--no-sandbox` as root, and attaches with `connect_over_cdp`; `_teardown` stops
  Playwright, the engine and removes the profile. `_goto` also relaunches when the
  engine process has exited.

## Integration

### HTTP surface (`main.py`)

Patch-selection contract: default `GET /api/ui/patches/{package_id}` remains the
legacy flat shape; `?format=grouped` opts into repo groups and stored/resolved/
unresolved state. `POST /api/ui/add_app` accepts legacy lists or repo mappings;
mapping is full replacement, while omitted existing exclusions are preserved.
See [README API notes](../README.md#patch-selection-api) and
`tests/test_grouped_patch_api.py` for the HTTP contract.

| Group | Routes | Notes |
|---|---|---|
| UI page | `GET /` | Renders `index.html` with `config`, `available_apps`, `metadata` |
| Apps | `GET /api/ui/patches/{package_id}`, `POST /api/ui/add_app`, `POST /api/ui/delete_app`, `POST /api/ui/upload_apk` | `add_app` also edits; it merges over the existing entry to keep pin, status and last build |
| Sources | `POST /api/ui/add_source`, `toggle_source`, `delete_source`, `GET /api/ui/discovery` | `add_source` syncs first with `force=True`, judges success by files on disk, and re-checks for a duplicate inside the config update. `POST /api/ui/discovery/sync` queues `update_tools_task`; `/api/ui/source_updates` was deleted |
| Status | `GET /api/ui/updates`, `build_status`, `tools_status`, `logs`, `metadata` | `updates` blocks on the worker for unpinned apps; returns the stale cache while a build or refresh is active |
| Build | `POST /api/ui/trigger_build` | `package_ids` null or empty means all apps |
| Settings | `POST /api/ui/save_settings`, `POST /api/ui/test_notification`, `GET /api/ui/secrets`, `PUT`/`DELETE /api/ui/secrets/{slug}`, `PUT`/`DELETE /api/ui/secrets/apprise-urls`, `POST /api/ui/secrets/apprise-urls/migration` | General settings reject non-empty notification URL bodies; tests use the effective stored list. Apprise status is count/source/migration metadata only; explicit migration removes legacy config only after secret persistence. The UI card currently exposes the GitHub field, not Apprise URL editing. Token changes report sync scheduling in `X-Sync` |
| Obtainium | `GET /view/{package_id}`, `GET /api/apps`, `GET /api/apps/{obtainium_id}`, `GET /download/{package_name}` | `/view` accepts package id or `obtainium_id`. `/download` takes an exact filename or a package id and serves the newest APK of exactly that package, or 404. Unknown ids on `/api/apps/{id}` give 404 |
| Static | `/icons` | `DATA_DIR/apks/icons` |
| Static | `/static` | `src/static`: `app.css` (URL carries a content hash) and the vendored Alpine.js |

No authentication on any route. Host/Origin/Fetch Metadata request guards are not
authentication. `ALLOWED_HOSTS` bare names allow a Host only; full origins allow
the exact scheme/host/port as an Origin and replace `settings.server_url` as a trust
anchor. Reverse proxies must preserve the original Host; `X-Forwarded-Host` is not
trusted. See [README host/proxy guidance](../README.md#host-and-reverse-proxy-checks).

### External

- GitHub REST (`/releases/latest`) and `raw.githubusercontent.com`, unauthenticated.
- APKMirror through ShardX; Morphe API (`api.morphe.software`, `resolve_variant_url` returns a `ResolvedVariant` tagged APKMirror, Uptodown or APKPure; APKPure is a label only, `apkeep` does the download) for variant resolution; Uptodown (`*.uptodown.com`, files from `uptodown.com`/`.net`) through the same browser as second source; APKPure via `apkeep` as last fallback.
- `java` subprocesses for `morphe-cli.jar` and `APKEditor.jar` (`settings.java_path`
  overrides the binary).
- Apprise for notifications, resolving `APPRISE_URLS` before the legacy
  `settings.notification_urls` runtime fallback.

### Consumers

- `templates/index.html` calls the `/api/ui/*` routes.
- Obtainium consumes `/view`, `/api/apps*` and `/download`.
- `tests/test_sources.py` imports `main` and `patcher`; `tests/test_scraper.py` imports
  `apkmirror_downloader` and `patcher`.
- `python -m src.patcher` runs `run_pipeline()` standalone;
  `python -m src.apkmirror_downloader` runs a manual download smoke test.
