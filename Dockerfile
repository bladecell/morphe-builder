FROM python:3.12-slim-bookworm

# Java 21 for Morphe CLI (class file 65); bookworm only packages 17
COPY --from=eclipse-temurin:21-jre /opt/java/openjdk /opt/java/openjdk
ENV JAVA_HOME=/opt/java/openjdk PATH=/opt/java/openjdk/bin:$PATH

# Essential system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy only dependency files first to leverage Docker layer caching
COPY pyproject.toml uv.lock ./

# Install project dependencies and the shared libs Chromium needs (ShardX's engine
# links the same libs). Playwright's own browser is not downloaded: the app only
# attaches to ShardX over CDP.
RUN uv sync --frozen && \
    uv run playwright install-deps chromium

# apkeep: APKPure fallback when APKMirror's Cloudflare challenge blocks the browser
RUN case "$(uname -m)" in \
      x86_64) arch=x86_64; sum=a23579a3ba366d25a6d69848189b983d65662f4ecf4b9e11e16510811659de4e ;; \
      aarch64) arch=aarch64; sum=5410acebd1b69427adcf98ccfdda6fa4dd3201e0540e5e2c01037b68e0a84049 ;; \
      *) echo "unsupported arch" >&2; exit 1 ;; \
    esac && \
    curl -fsSL -o /usr/local/bin/apkeep \
      "https://github.com/EFForg/apkeep/releases/download/1.0.0/apkeep-${arch}-unknown-linux-gnu" && \
    echo "${sum}  /usr/local/bin/apkeep" | sha256sum -c - && \
    chmod +x /usr/local/bin/apkeep

# Copy the entire source code
COPY src/ src/

# Setup persistent data directory and environment
ENV DATA_DIR=/data
ENV HOME=/root
RUN mkdir -p /data

# Pre-fetch the ShardX engine at build time so nothing is downloaded at runtime.
# XDG_CACHE_HOME keeps the engine outside /data (volume mounts cannot shadow it);
# the SDK resolves its cache to $XDG_CACHE_HOME/shardx-sdk. The archives are
# sha256-pinned (the CDN's multipart ETags are not content hashes); the build
# fails on mismatch. To update: re-download, sha256sum, replace the pins below.
ENV XDG_CACHE_HOME=/opt/xdg-cache
RUN uv run python -c "import hashlib,sys,zipfile; from shardx import ShardX, runtime as r; \
P={'.ShardX-Linux.zip.tmp':'3ead1629a1c893f248cd01ee7c9125552b2cac5c91846cb71d6cb1b24883c213','.ShardX-Widevine-Linux.zip.tmp':'44fb1a3afd884d650904b88637eef8cede162ea5d302292a7b8813deafb751dc','bundle.zip':'6ed51c9571b0100a5fa56b578cbc6e118b3d47eb5182759d66be4f55712bb66f'}; seen=set(); \
chk=lambda p:(seen.add(str(p)),None)[1] if hashlib.sha256(open(p,'rb').read()).hexdigest()==P[__import__('os').path.basename(p)] else sys.exit('sha256 mismatch: '+str(p)); \
r._system_unzip=lambda a,d,o=r._system_unzip:(chk(a),o(a,d))[1]; \
r.zipfile.ZipFile=lambda p,*a,o=zipfile.ZipFile:(chk(p),o(p,*a))[1]; \
sdk=ShardX(); sdk.runtime.install(); \
assert len(seen)==3,seen; assert sdk.runtime.installed; print('ShardX engine installed at',sdk.runtime.root)" && \
    ldd /opt/xdg-cache/shardx-sdk/ShardX-Linux/chrome | (! grep "not found")

# Expose the dashboard port
EXPOSE 8000

# Start the application using uvicorn
# The persistent ScraperWorker thread will be initialized by the FastAPI startup event
CMD ["uv", "run", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
