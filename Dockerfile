FROM python:3.12-slim-bookworm

# Install Java 17 and system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jre-headless \
    curl \
    ca-certificates \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Sync dependencies and install Playwright browsers with system deps
RUN uv sync && \
    uv run playwright install --with-deps chromium

# Copy source only
COPY src/ src/

# Setup persistent data directory
ENV DATA_DIR=/data
RUN mkdir -p /data

# Expose port
EXPOSE 8000

# The app handles generating a default config.yaml in /data if missing
CMD ["uv", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
