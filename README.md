# Morphe Builder

Morphe Builder is an automated Android application patcher with a Web UI and Obtainium integration. It allows you to automatically download, patch, and serve Android apps with custom patches from various sources.

## Features

- **Automated Pipeline:** Downloads raw APKs (including bundles/XAPKs), merges them, and applies patches using [Morphe CLI](https://github.com/MorpheApp/morphe-cli).
- **HushFacebook compatibility:** Facebook builds that select `SysAdminDoc/HushFacebook` require APKs with `minSdkVersion` 30 or higher; cached APKs and manual uploads are checked before use. This enforces the Android minimum SDK, not DEX-variant identity.
- **Web UI:** Manage your app pipeline, discover new compatible apps from patch sources, and monitor build logs in real-time.
- **Update Tracking:** Automatically checks for new versions of apps and patches.
- **Obtainium Integration:** Serves an API compatible with Obtainium for easy installation and updates on Android devices.
- **Cron Scheduling:** Supports scheduled background builds to keep your apps up-to-date automatically.

## Prerequisites

- **Docker:** (Recommended) No other dependencies are required.
- **Manual Installation:**
  - Python 3.12+
  - Java 17+ (required for [Morphe CLI](https://github.com/MorpheApp/morphe-cli) and APKEditor)

## Getting Started

### Using Docker (Recommended)

The easiest way to run Morphe Builder is with Docker. All persistent data (config, APKs, and tools) is stored in the `/data` directory.

The Web UI and APIs have **no application authentication**. Do not expose the
container directly to an untrusted network. Restrict access at the network or
reverse-proxy layer and use HTTPS; otherwise credentials submitted through the
UI cross the network without transport encryption. The request guards are not
an authentication system.

#### 1. Use Docker Hub Image (Easiest)

Pull and run the pre-built image from Docker Hub:

```bash
docker run -d \
  -p 8000:8000 \
  -v $(pwd)/data:/data \
  --name morphe-builder \
  bladecell/morphe-builder:latest
```

#### 2. Build and Run Locally

If you prefer to build the image yourself:

```bash
docker build -t morphe-builder .

# Run with a persistent volume for all data
docker run -d \
  -p 8000:8000 \
  -v $(pwd)/data:/data \
  --name morphe-builder \
  morphe-builder
```

### Manual Installation

1. Install dependencies:
   ```bash
   uv sync
   ```
2. Run the application:
   ```bash
   uv run uvicorn src.main:app --host 0.0.0.0 --port 8000
   ```

## Development

### Page styles and scripts

The admin page loads nothing from a third party. Its stylesheet,
`src/static/app.css`, is built ahead of time with the Tailwind CSS 3.4.17
standalone CLI and committed. Alpine.js 3.17.4 is vendored as
`src/static/alpine-3.17.4.min.js`. Licences are in `THIRD_PARTY_NOTICES`.

After changing `src/templates/*.html`, `tailwind/tailwind.config.js` or
`tailwind/input.css`, rebuild the stylesheet:

```bash
uv run python scripts/build_css.py
```

Commit `src/static/app.css` and `tailwind/inputs.sha256` together with the
change. The tests fail with a hint to run this command when the stylesheet is
out of date.

The first run downloads the Tailwind binary for your platform into
`.cache/tailwind/` (gitignored) and checks it against the SHA-256 pinned in
`scripts/build_css.py`; a file that does not match is not run. Linux and macOS
on x64 and arm64 are supported. Node.js is not needed, and the Docker image
needs no build step because it copies `src/static/`.

To upgrade Tailwind, change `TAILWIND_VERSION` and the hashes in
`scripts/build_css.py` from the release's `sha256sums.txt`, then rebuild. To
upgrade Alpine, take `dist/cdn.min.js` from the npm package, check it against
the registry's integrity value, rename it with the new version, and update the
tag in `src/templates/index.html`, the hash in `tests/test_static_assets.py`
and `THIRD_PARTY_NOTICES`.

## Configuration

Settings are managed via `config.yaml` in the root directory or through the Web UI's Settings tab.

### Runtime environment

Docker accepts runtime environment variables without rebuilding the image. For
example, export any secrets in the deployment environment and pass them through
by name (the command does not contain their values):

```bash
docker run -d \
  -p 8000:8000 \
  -v "$(pwd)/data:/data" \
  -e GITHUB_TOKEN \
  -e APPRISE_URLS \
  -e ALLOWED_HOSTS='builder.example.net,https://builder.example.net' \
  --name morphe-builder \
  bladecell/morphe-builder:latest
```

Omit either of `GITHUB_TOKEN` or `APPRISE_URLS` when
not configured. `APPRISE_URLS`, when supplied, must be a JSON array of strings.
Use a deployment secret manager or a protected host environment to supply
values; do not put real values in shell history, compose files committed to a
repository, or this documentation.

### Secrets

`GITHUB_TOKEN` and notification URLs are
write-only secret-store entries. The Settings Secrets card exposes the token;
notification URL management is available through
`PUT`/`DELETE /api/ui/secrets/apprise-urls`, with an explicit legacy import at
`POST /api/ui/secrets/apprise-urls/migration`. Status endpoints report count,
source and migration state, never URL values; other secret statuses report
whether a value is set and its source, never the value. Only GitHub-token
changes can schedule a source sync.

Without an environment value, the app stores secrets in `DATA_DIR/.env`
(`./.env` for a local install, `/data/.env` in Docker). It writes this file
atomically with mode `0600`; notification URLs are stored as a JSON array under
`APPRISE_URLS`. The file is plaintext, so protect the data directory and
its backups. Do not configure this application file as Docker `--env-file`,
Compose `env_file`, `uvicorn --env-file`, or `source` it in a shell. At startup
the app absorbs environment values into memory and removes them from its process
environment. Environment values take precedence over file values and cannot be
changed through the UI. An invalid environment value is ignored, reported as
invalid, and does **not** fall back to the file.

The GitHub token is sent only as an authorization header to `api.github.com`;
it is not used for APKMirror or passed to child processes. Prefer a token with
no permissions: patch bundles run in subprocesses and can otherwise gain access
to permissions held by the token. GitHub's scope header can report permissions
for classic tokens; fine-grained and GitHub App token permissions are not
verifiable by this status check.

### Browser and APK sources

APKMirror is scraped with [ShardX](https://github.com/ProxyShard/ShardBrowser)
(`shardx==2.0.3`, MIT), a patched Chromium. No licence key is needed. The app
launches the engine headless, attaches Playwright over CDP, and uses a throwaway
profile per launch (`--no-sandbox` when running as root). The engine (about
170 MB) is downloaded from a ProxyShard CDN, unsigned, at Docker build time
only; see the Dockerfile. It replaced cloakbrowser because that browser could
not pass APKMirror's Cloudflare challenge in Docker.

An old `CLOAKBROWSER_LICENSE_KEY` is removed from `.env` and the environment at
startup, and the retired `DATA_DIR/bin/cloakbrowser` cache is deleted.

The app first asks the Morphe API to resolve the APKMirror variant page for an
exact version. If APKMirror cannot be downloaded from, it falls back to APKPure
through `apkeep` (pinned by SHA-256 in the Dockerfile).

### Host and reverse-proxy checks

`ALLOWED_HOSTS` is a comma-separated list. A bare name such as
`builder.example.net` permits that HTTP `Host` name. It does not add a separate
Origin trust entry, though the normal same-host/same-port Origin check still
applies. A full origin such as `https://builder.example.net` permits the
matching host and that exact Origin (including scheme and port). Once a
full-origin entry is present, `settings.server_url` is not a trust anchor;
configure the full public origin explicitly. With `ALLOWED_HOSTS` unset, the
configured `settings.server_url` continues to provide the host/origin trust
anchor.

Pass the original `Host` header through the reverse proxy (nginx example:
`proxy_set_header Host $host;`; Caddy and Traefik do this by default). The app
does not trust `X-Forwarded-Host`. For a TLS-terminating proxy, list the public
HTTPS origin, as in the Docker example. `ALLOWED_HOSTS` helps reject unexpected
Host/Origin requests; it does not replace TLS, network access controls, or
authentication at the proxy.

## Patch selection API

`GET /api/ui/patches/{package_id}` keeps its legacy flat-list response by default.
The UI opts into source groups with `?format=grouped`; the catalog reports source
repo identity, patch entries and stored/resolved/unresolved selection state. A
partially unreadable catalog returns HTTP 200 with per-source status and fixed
selection messages. Unsupported `format` values return HTTP 400. An unreadable
selected source may instead produce HTTP 502 with a fixed error.

`POST /api/ui/add_app` accepts `include_patches` as either the legacy flat list
or a repo-to-patch-name mapping. Flat lists remain flat and resolve duplicate
names by the first active provider in config order. Mapping saves are full
replacements: omitted entries are removed, so the UI includes retained inactive
or deleted-source selections until the operator explicitly removes them. An
existing app's exclusions are preserved when omitted from the request. Invalid
or newly unresolvable active selections return HTTP 400 with
`{"detail":"Invalid patch selection"}`. These unauthenticated UI endpoints
have no API scopes or documented rate limit; protect access at the network or
reverse-proxy layer as described above.

Example mapped save:

```json
{
  "package_id": "com.example.app",
  "name": "Example",
  "include_patches": {
    "owner/patches": ["Patch A", "Patch B"]
  }
}
```

An inactive/deleted source or unchanged stale selection is retained and shown
for explicit cleanup; builds warn and skip unavailable mappings and may fail if
no applicable source remains. A partial catalog is not a guarantee that every
patch was loaded.

## Obtainium Setup

To add an app to Obtainium:
1. Open the Morphe Builder Web UI.
2. In the **Pipeline** tab, click the **+ (Plus)** icon on an app card.
3. If you have Obtainium installed, it will prompt to add the app using the generated API link.

Alternatively, add the following URL to Obtainium:
`http://<your-server-ip>:8000/api/apps/<obtainium_id>`
