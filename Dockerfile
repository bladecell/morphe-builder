FROM python:3.12-slim-bookworm

# Install Java 17 and essential system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jre-headless \
    curl \
    ca-certificates \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy only dependency files first to leverage Docker layer caching
COPY pyproject.toml uv.lock ./

# Install project dependencies and Playwright/Chromium with its own system deps
RUN uv sync --frozen && \
    uv run playwright install --with-deps chromium

# Copy the entire source code
COPY src/ src/

# Setup persistent data directory and environment
ENV DATA_DIR=/data
ENV HOME=/root
RUN mkdir -p /data

# Pre-initialize cloakbrowser binaries to prevent runtime delays/errors
RUN uv run python -c "from cloakbrowser.download import ensure_binary; ensure_binary()"

# Expose the dashboard port
EXPOSE 8000

# Start the application using uvicorn
# The persistent ScraperWorker thread will be initialized by the FastAPI startup event
CMD ["uv", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
