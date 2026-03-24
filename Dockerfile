FROM python:3.12-slim-bookworm

# Install Java 17 and system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jre-headless \
    curl \
    ca-certificates \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install apkeep (Rust-based downloader) - Architecture aware
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then \
        DOWNLOAD_ARCH="x86_64-unknown-linux-gnu"; \
    elif [ "$ARCH" = "aarch64" ]; then \
        DOWNLOAD_ARCH="aarch64-unknown-linux-gnu"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    curl -L "https://github.com/EFForg/apkeep/releases/latest/download/apkeep-$DOWNLOAD_ARCH" -o /usr/local/bin/apkeep \
    && chmod +x /usr/local/bin/apkeep

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Sync dependencies
RUN uv sync

# Copy source only
COPY src/ src/

# Setup persistent data directory
ENV DATA_DIR=/data
RUN mkdir -p /data

# Expose port
EXPOSE 8000

# The app handles generating a default config.yaml in /data if missing
CMD ["uv", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
