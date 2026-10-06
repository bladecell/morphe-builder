# Morphe Builder

Morphe Builder is an automated Android application patcher with a Web UI and Obtainium integration. It allows you to automatically download, patch, and serve Android apps with custom patches from various sources.

## Features

- **Automated Pipeline:** Downloads raw APKs (including bundles/XAPKs), merges them, and applies patches using [Morphe CLI](https://github.com/MorpheApp/morphe-cli).
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

## Obtainium Setup

To add an app to Obtainium:
1. Open the Morphe Builder Web UI.
2. In the **Pipeline** tab, click the **+ (Plus)** icon on an app card.
3. If you have Obtainium installed, it will prompt to add the app using the generated API link.

Alternatively, add the following URL to Obtainium:
`http://<your-server-ip>:8000/api/apps/<obtainium_id>`


