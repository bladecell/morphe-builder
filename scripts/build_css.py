"""Build the admin page stylesheet with the pinned Tailwind standalone CLI.

Run from anywhere: ``uv run python scripts/build_css.py``.

The script downloads the Tailwind 3.4.17 binary for this platform into
``.cache/tailwind/`` (gitignored), checks its SHA-256 against the value pinned
below before it is made executable or run, then writes ``src/static/app.css`` and
records a digest of the build inputs in ``tailwind/inputs.sha256``. Both outputs
are committed; ``tests/test_static_assets.py`` fails when the inputs change
without a rebuild.
"""

import hashlib
import logging
import os
import platform
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".cache" / "tailwind"
HASH_RECORD = ROOT / "tailwind" / "inputs.sha256"
STYLESHEET = ROOT / "src" / "static" / "app.css"
TAILWIND_CONFIG = Path("tailwind") / "tailwind.config.js"
INPUT_CSS = Path("tailwind") / "input.css"
# Everything the CLI is told except the binary and the output path. Part of the
# digest, so changing it without a rebuild fails the staleness test.
CLI_ARGUMENTS = ["-c", str(TAILWIND_CONFIG), "-i", str(INPUT_CSS), "--minify"]
# The binaries are about 40 to 50 MB; stop a wrong URL from filling the disk.
MAX_BINARY_BYTES = 150 * 1024 * 1024

TAILWIND_VERSION = "3.4.17"
RELEASE_URL = (
    f"https://github.com/tailwindlabs/tailwindcss/releases/download/v{TAILWIND_VERSION}"
)
# From the release's sha256sums.txt; the linux-x64 value was also recomputed from
# the downloaded binary.
BINARY_SHA256 = {
    "linux-x64": "7d24f7fa191d2193b78cd5f5a42a6093e14409521908529f42d80b11fde1f1d4",
    "linux-arm64": "69b1378b8133192d7d2feb12a116fa12d035594f58db3eff215879e4ad8cf39b",
    "macos-x64": "6cbdad74be776c087ffa5e9a057512c54898f9fe8828d3362212dfe32fc933a3",
    "macos-arm64": "a1d0c7985759accca0bf12e51ac1dcbf0f6cf2fffb62e6e0f62d091c477a10a3",
}
_SYSTEMS = {"Linux": "linux", "Darwin": "macos"}
_MACHINES = {
    "x86_64": "x64",
    "amd64": "x64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


class UnsupportedPlatformError(RuntimeError):
    """No pinned Tailwind binary exists for this platform."""


class BinaryHashError(RuntimeError):
    """A Tailwind binary does not match its pinned SHA-256."""


class DownloadTooLargeError(OSError):
    """A download went past its byte cap."""


def current_platform() -> tuple[str, str]:
    """Return `(system, machine)` of the running interpreter."""
    return platform.system(), platform.machine()


def platform_key(system: str, machine: str) -> str:
    """Name the pinned binary for a platform.

    Args:
        system: `platform.system()` value, for example `Linux`.
        machine: `platform.machine()` value, for example `x86_64`.

    Returns:
        A key of `BINARY_SHA256`, for example `linux-x64`.

    Raises:
        UnsupportedPlatformError: If no binary is pinned for the platform.
    """
    os_name = _SYSTEMS.get(system)
    arch = _MACHINES.get(machine.lower())
    if os_name is None or arch is None:
        raise UnsupportedPlatformError(
            f"No Tailwind binary is pinned for {system} {machine}. "
            f"Supported: {', '.join(sorted(BINARY_SHA256))}."
        )
    return f"{os_name}-{arch}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _binary_path(key: str, cache_dir: Path) -> Path:
    return cache_dir / f"tailwindcss-{TAILWIND_VERSION}-{key}"


def cached_binary(key: str, cache_dir: Path) -> Path | None:
    """Return the cached binary if it exists and matches its pinned hash.

    Never downloads. A cached file with a different hash is not returned.

    Args:
        key: Platform key from `platform_key`.
        cache_dir: Directory holding cached binaries.

    Returns:
        The verified binary path, or None.
    """
    path = _binary_path(key, cache_dir)
    if path.is_file() and _file_sha256(path) == BINARY_SHA256[key]:
        return path
    return None


def download_file(
    url: str, destination: Path, max_bytes: int = MAX_BINARY_BYTES
) -> None:
    """Download `url` to `destination`, giving up past `max_bytes`.

    Args:
        url: HTTPS URL to fetch.
        destination: File to create or overwrite. A partial file may remain when
            this raises; the caller deletes it.
        max_bytes: Most bytes to accept.

    Raises:
        DownloadTooLargeError: If the body is larger than `max_bytes`.
        OSError: On a network or file error (`urllib.error.URLError` is one).
    """
    received = 0
    with (
        urllib.request.urlopen(url, timeout=120) as response,
        destination.open("wb") as handle,
    ):
        while chunk := response.read(1 << 20):
            received += len(chunk)
            if received > max_bytes:
                raise DownloadTooLargeError(
                    f"Download of {url} exceeded {max_bytes} bytes; aborted."
                )
            handle.write(chunk)


def ensure_binary(
    key: str, cache_dir: Path, download: Callable[[str, Path], None]
) -> Path:
    """Return a hash-verified Tailwind binary, downloading it if not cached.

    The download goes to a temporary file that is verified before it is renamed
    into place and made executable. A file that fails the check is deleted.

    Args:
        key: Platform key from `platform_key`.
        cache_dir: Directory holding cached binaries; created if missing.
        download: Callable `(url, destination)` that fetches the file.

    Returns:
        Path of an executable binary whose SHA-256 matches the pinned value.

    Raises:
        BinaryHashError: If the cached or downloaded file does not match.
    """
    expected = BINARY_SHA256[key]
    target = _binary_path(key, cache_dir)
    if target.is_file():
        if _file_sha256(target) != expected:
            raise BinaryHashError(
                f"Cached {target} does not match the pinned SHA-256 for {key}. "
                "Delete it and run again."
            )
        return target
    cache_dir.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    try:
        download(f"{RELEASE_URL}/tailwindcss-{key}", partial)
        actual = _file_sha256(partial)
        if actual != expected:
            raise BinaryHashError(
                f"Downloaded Tailwind binary for {key} has SHA-256 {actual}, "
                f"expected {expected}. The download may be incomplete or tampered "
                "with; it was deleted. Run the script again to retry."
            )
        partial.chmod(0o755)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def inputs_digest(root: Path) -> str:
    """Hash everything the stylesheet is built from.

    Covers every `src/templates/*.html` (the files the scanner reads), the
    Tailwind config and the input CSS, by relative path and content, plus the
    pinned Tailwind version and the CLI arguments.

    Args:
        root: Repository root.

    Returns:
        Hex SHA-256 digest.
    """
    files = sorted((root / "src" / "templates").glob("*.html"))
    files += [root / TAILWIND_CONFIG, root / INPUT_CSS]
    digest = hashlib.sha256()
    digest.update(f"tailwind {TAILWIND_VERSION}\0".encode())
    digest.update("\0".join(CLI_ARGUMENTS).encode() + b"\0")
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def build(binary: Path, root: Path, output: Path) -> None:
    """Run the Tailwind CLI on the repository's templates.

    Args:
        binary: Verified Tailwind executable.
        root: Repository root; the CLI runs there so content paths resolve.
        output: Stylesheet to write.

    Raises:
        subprocess.CalledProcessError: If the CLI fails.
    """
    command = [str(binary), *CLI_ARGUMENTS, "-o", str(output)]
    # The embedded browserslist data is old by design; its warning is noise.
    env = {**os.environ, "BROWSERSLIST_IGNORE_OLD_DATA": "1"}
    subprocess.run(command, cwd=root, env=env, check=True, capture_output=True)


def main(argv: list[str] | None = None) -> int:
    """Download and verify the binary, build the stylesheet, record the digest.

    Args:
        argv: Unused; the script takes no arguments.

    Returns:
        Process exit status: 0 on success, 1 on any failure.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    try:
        key = platform_key(*current_platform())
        binary = ensure_binary(key, CACHE_DIR, download_file)
        # Next to the binary, inside the repo so the final rename stays on one
        # filesystem, but outside src/static/, which the app serves.
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        temporary = CACHE_DIR / "app.css.tmp"
        try:
            build(binary, ROOT, temporary)
            os.replace(temporary, STYLESHEET)
        finally:
            temporary.unlink(missing_ok=True)
        HASH_RECORD.write_text(inputs_digest(ROOT) + "\n")
    except (UnsupportedPlatformError, BinaryHashError) as error:
        logger.error("%s", error)
        return 1
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", b"") or b""
        logger.error("Build failed: %s %s", error, detail.decode(errors="replace"))
        return 1
    logger.info("Wrote %s (%d bytes)", STYLESHEET, STYLESHEET.stat().st_size)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
