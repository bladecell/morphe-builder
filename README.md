# Morphe Builder

Morphe Builder is an automated Android application patcher with a Web UI and Obtainium integration. It allows you to automatically download, patch, and serve Android apps with custom patches from various sources.

## Features

- **Automated Pipeline:** Downloads raw APKs (including bundles/XAPKs), merges them, and applies patches using Morphe CLI.
- **Web UI:** Manage your app pipeline, discover new compatible apps from patch sources, and monitor build logs in real-time.
- **Update Tracking:** Automatically checks for new versions of apps and patches.
- **Obtainium Integration:** Serves an API compatible with Obtainium for easy installation and updates on Android devices.
- **Cron Scheduling:** Supports scheduled background builds to keep your apps up-to-date automatically.

## Prerequisites

- **Docker:** (Recommended) No other dependencies are required.
- **Manual Installation:**
  - Python 3.12+
  - Java 17+ (required for Morphe CLI and APKEditor)
  - [apkeep](https://github.com/EFForg/apkeep) (for downloading APKs)

## Getting Started

### Using Docker (Recommended)

The easiest way to run Morphe Builder is with Docker. All persistent data (config, APKs, and tools) is stored in the `/data` directory.

#### 1. Docker Run

```bash
docker build -t morphe-builder .

# Run with a persistent volume for all data
docker run -d \
  -p 8000:8000 \
  -v $(pwd)/data:/data \
  --name morphe-builder \
  morphe-builder
```

#### 2. Docker Compose

Create a `docker-compose.yml` file:

```yaml
services:
  morphe-builder:
    build: .
    container_name: morphe-builder
    ports:
      - "8000:8000"
    volumes:
      - ./data:/data
    restart: unless-stopped
```

Then run:
```bash
docker compose up -d
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

## Configuration

Settings are managed via `config.yaml` in the root directory or through the Web UI's Settings tab.

## Obtainium Setup

To add an app to Obtainium:
1. Open the Morphe Builder Web UI.
2. In the **Pipeline** tab, click the **+ (Plus)** icon on an app card.
3. If you have Obtainium installed, it will prompt to add the app using the generated API link.

Alternatively, add the following URL to Obtainium:
`http://<your-server-ip>:8000/api/apps/<obtainium_id>`

## License

MIT
