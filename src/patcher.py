import builtins
import requests
import re
import subprocess
import os
import json
import time
import tempfile
import hashlib
import uuid
import threading
import shutil
import sys
import logging
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Sequence
from typing import Any, TextIO
from urllib.parse import quote, urljoin, urlsplit
from packaging import version
from src.apkmirror_downloader import (
    ApkMirror_Downloader,
    AndroidVariantMetadataError,
    AndroidVariantInspectionError,
    AndroidVariantUnavailableError,
    Arch,
    BundleType,
    Dpi,
)

from src import secrets_store
from src.config_store import DATA_DIR, load_config, update_config

logger = logging.getLogger(__name__)
secrets_store.install_log_redaction()


def print(
    *values: object,
    sep: str | None = " ",
    end: str | None = "\n",
    file: TextIO | None = None,
    flush: bool = False,
) -> None:
    """Print like the builtin, with secret values removed from the text.

    This module reports progress and failures with plain prints, and some of
    them include exception text. Shadowing the builtin covers every print made
    by this module and nothing else: other modules print through their own
    means, and a direct `sys.stdout.write` here must call `secrets_store.redact`
    itself (the java output in `patch_app` does). While a web build runs, the
    build log also redacts what it captures.

    Args:
        *values: Objects to print, as for the builtin.
        sep: Separator between values.
        end: Text added at the end.
        file: Stream to write to; the current `sys.stdout` when None.
        flush: Flush the stream after writing.
    """
    text = (" " if sep is None else sep).join(str(value) for value in values)
    builtins.print(secrets_store.redact(text), end=end, file=file, flush=flush)


def run_child(
    command: Sequence[str | Path], **kwargs: Any
) -> subprocess.CompletedProcess[Any]:
    """Run a subprocess without the secrets in its environment.

    Every subprocess of the app must be started through this or `start_child`
    (a test enforces it): patch bundles run as code inside java.

    Args:
        command: The program and its arguments.
        **kwargs: Keyword arguments of `subprocess.run`, except `env`.

    Returns:
        The completed process.

    Raises:
        TypeError: `env` was passed; the environment is not the caller's choice.
        subprocess.SubprocessError: As `subprocess.run`.
        OSError: As `subprocess.run`.
    """
    return subprocess.run(  # noqa: PLW1510 - callers choose `check`
        command, env=secrets_store.child_environment(), **kwargs
    )


def start_child(command: Sequence[str | Path], **kwargs: Any) -> subprocess.Popen[Any]:
    """Start a subprocess without the secrets in its environment.

    Args:
        command: The program and its arguments.
        **kwargs: Keyword arguments of `subprocess.Popen`, except `env`.

    Returns:
        The running process.

    Raises:
        TypeError: `env` was passed; the environment is not the caller's choice.
        OSError: As `subprocess.Popen`.
    """
    return subprocess.Popen(command, env=secrets_store.child_environment(), **kwargs)


VERSIONS_FILE = DATA_DIR / "bin" / "versions.json"
VERSION_CACHE_FILE = DATA_DIR / "bin" / "version_cache.json"


def load_local_versions():
    DATA_DIR.joinpath("bin").mkdir(parents=True, exist_ok=True)
    if VERSIONS_FILE.exists():
        with open(VERSIONS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_local_versions(versions):
    with open(VERSIONS_FILE, "w") as f:
        json.dump(versions, f, indent=4)


def load_version_cache():
    if VERSION_CACHE_FILE.exists():
        try:
            with open(VERSION_CACHE_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}


def save_version_cache(cache):
    VERSION_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(VERSION_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=4)


def _safe_version_sort_key(v):
    """Sort key that never throws on malformed version strings."""
    try:
        return (1, version.parse(v), "")
    except Exception:
        return (0, version.parse("0"), str(v))


def _no_netrc(request: requests.PreparedRequest) -> requests.PreparedRequest:
    """Auth hook that adds nothing.

    Passing any `auth` stops `requests` from reading `~/.netrc` for the host of
    one request. It does not cover a redirect (`requests` looks the netrc up
    again for every hop), so `_get_following` sends each hop itself.

    Args:
        request: The request about to be sent.

    Returns:
        The same request, unchanged.
    """
    return request


# Redirects followed for one download (a release asset normally redirects once).
MAX_DOWNLOAD_REDIRECTS = 5


def _get_following(url: str, **kwargs: Any) -> requests.Response:
    """GET a URL and follow redirects one hop at a time, never using `~/.netrc`.

    `requests.get` with `trust_env` on reads the operator's netrc on every
    redirect hop. Here each hop is its own `requests.get` with `auth` set and
    `allow_redirects=False`, so no hop can pick up netrc credentials. Proxies and
    the CA bundle still come from the environment, as `requests.get` reads them.
    Only https redirect targets are followed.

    Args:
        url: First URL.
        **kwargs: Passed to `requests.get` (for example `stream=True`).

    Returns:
        The first response that is not a redirect.

    Raises:
        requests.exceptions.TooManyRedirects: More than `MAX_DOWNLOAD_REDIRECTS`.
        requests.exceptions.InvalidURL: A redirect led to a non-https target.
        requests.exceptions.RequestException: The request failed.
    """
    target = url
    for _ in range(MAX_DOWNLOAD_REDIRECTS + 1):
        response = requests.get(
            target, timeout=30, auth=_no_netrc, allow_redirects=False, **kwargs
        )
        if not response.is_redirect:
            return response
        location = response.headers.get("location")
        response.close()
        target = urljoin(target, location or "")
        if not target.startswith("https://"):
            raise requests.exceptions.InvalidURL("Redirect to a non-https target")
    raise requests.exceptions.TooManyRedirects("Too many redirects")


def download_with_progress(url: str, path: Path) -> None:
    """Download a file with a progress bar, replacing `path` only when complete.

    The data goes to a temp file of its own next to `path`
    (`<name>.<random>.part`), so an interrupted download never leaves a truncated
    file at `path`, an existing file there stays intact, and two downloads of the
    same target cannot publish each other's half-written data.

    Args:
        url: URL to fetch.
        path: Final location of the file.

    Raises:
        requests.exceptions.RequestException: The request or the transfer failed.
        OSError: The file could not be written or moved into place.
    """
    partial_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.part")
    try:
        _stream_to_file(url, partial_path)
        os.replace(partial_path, path)
    finally:
        partial_path.unlink(missing_ok=True)


def _stream_to_file(url: str, path: Path) -> None:
    """Download a file to `path` with a text progress bar printed to stdout.

    Args:
        url: URL to fetch.
        path: File to write. It is overwritten, and left partial if the transfer
            fails; `download_with_progress` handles that.

    Raises:
        requests.exceptions.RequestException: The request or the transfer failed.
        OSError: The file could not be written.
    """
    response = _get_following(url, stream=True)
    response.raise_for_status()
    total_size = int(response.headers.get("content-length", 0))
    block_size = 1024 * 64  # 64KB

    downloaded = 0
    last_percent = -1
    with open(path, "wb") as f:
        for data in response.iter_content(block_size):
            f.write(data)
            downloaded += len(data)
            if total_size > 0:
                percent = int(100 * downloaded / total_size)
                if percent != last_percent:
                    last_percent = percent
                    bar_length = 30
                    filled_length = int(bar_length * downloaded // total_size)
                    bar = "\u2588" * filled_length + "-" * (bar_length - filled_length)
                    # \x1b[2K clears the line to prevent trailing chars from previous updates
                    sys.stdout.write(
                        f"\r\x1b[2K    [{bar}] {percent}% ({downloaded / 1024 / 1024:.1f}MB / {total_size / 1024 / 1024:.1f}MB)"
                    )
                    sys.stdout.flush()
    sys.stdout.write("\n")
    sys.stdout.flush()


# How often (seconds) each repo may hit the GitHub API for update checks.
# Prevents every build from burning the unauthenticated 60 req/hour limit.
UPDATE_CHECK_INTERVAL = 6 * 3600

# Candidate JSON filenames shipped by patch sources (Morphe vs ReVanced style).
JSON_FILENAME_CANDIDATES = ["patches-list.json", "patches.json"]


class GitHubRateLimitError(Exception):
    """Raised when the GitHub API refuses a request because of a rate limit.

    Attributes:
        reset_epoch: Reset time in epoch seconds, or None when GitHub gave none.
        suggest_token: True when the message points at the GitHub token setting
            (no token was in use).
    """

    def __init__(
        self, reset_epoch: float | None = None, suggest_token: bool = False
    ) -> None:
        self.reset_epoch = reset_epoch
        self.suggest_token = suggest_token
        if reset_epoch is None:
            when = "Try again later."
        else:
            reset = datetime.fromtimestamp(reset_epoch, tz=UTC)
            when = f"Resets at {reset:%Y-%m-%d %H:%M} UTC."
        hint = f" {RATE_LIMIT_TOKEN_HINT}" if suggest_token else ""
        super().__init__(f"GitHub API rate limit reached. {when}{hint}")


class GitHubHostError(ValueError):
    """A GitHub API call was asked for a target other than https://api.github.com."""


class GitHubApiError(requests.exceptions.RequestException):
    """An authenticated GitHub API request failed.

    The message is fixed text plus the failure's class name, never the original
    error text: that text can echo request headers, and so the token.
    """


class GitHubTokenRejectedError(Exception):
    """GitHub answered 401: the saved token is invalid, expired or revoked.

    The message is fixed. It holds neither the token nor the response body.
    """

    def __init__(self) -> None:
        super().__init__(TOKEN_REJECTED_MESSAGE)


TOKEN_REJECTED_MESSAGE = (
    "GitHub rejected the saved GitHub token (HTTP 401). "
    "Replace or remove the GitHub token in Settings."
)
RATE_LIMIT_TOKEN_HINT = "Set a GitHub token in Settings to raise the limit."
GITHUB_API_HOST = "api.github.com"
# Redirects the API may send inside api.github.com (a renamed repository).
MAX_API_REDIRECTS = 3
# A real scope looks like `repo`, `admin:org`, `read:packages`, `delete_repo`.
# The response header is untrusted text: only such short names are kept.
_SCOPE_NAME = re.compile(r"[a-z][a-z0-9_:]{0,31}")
_MAX_SCOPE_NAMES = 20
_MAX_SCOPE_HEADER_CHARS = 1024


@dataclass(frozen=True)
class _TokenState:
    """What the last authenticated GitHub request showed about one token.

    Attributes:
        fingerprint: SHA-256 of the token that was used. It ties the state to
            that token without keeping the token (or anything that is it).
        scopes: Scope names GitHub reported, or None (none reported).
        rejected: True when GitHub answered 401 for this token.
    """

    fingerprint: bytes
    scopes: tuple[str, ...] | None
    rejected: bool


# The state of the token last used, or None when none was used yet or the last
# request had no token. It only counts while the token in effect has the same
# fingerprint, so replacing or clearing the token drops it.
_token_state: _TokenState | None = None


def _fingerprint(token: str) -> bytes:
    """Return a digest that identifies a token without holding it.

    Args:
        token: The GitHub token.

    Returns:
        Its SHA-256 digest.
    """
    return hashlib.sha256(token.encode()).digest()


def _is_api_url(url: object) -> bool:
    """Tell whether `url` is exactly an https URL on api.github.com.

    Args:
        url: Candidate URL.

    Returns:
        True only for the https scheme, host api.github.com, default port and no
        user info. Anything with spaces, control characters, backslashes or
        non-ASCII text is refused, so parsers cannot disagree about the host.
    """
    if not isinstance(url, str) or not url.isascii() or not url.isprintable():
        return False
    if " " in url or "\\" in url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and parts.netloc.lower() == GITHUB_API_HOST


def _parse_scopes(header: str | None, token: str) -> tuple[str, ...] | None:
    """Pick the plain scope names out of an `X-OAuth-Scopes` header.

    Args:
        header: Header value, or None when GitHub sent none (a fine-grained
            token, whose permissions are not reported this way).
        token: The token used; a name equal to it is dropped.

    Returns:
        Up to `_MAX_SCOPE_NAMES` names that look like scopes, or None when there
        was no header or it was longer than `_MAX_SCOPE_HEADER_CHARS`.
    """
    if header is None or len(header) > _MAX_SCOPE_HEADER_CHARS:
        return None
    names = (part.strip() for part in header.split(","))
    plain = [n for n in names if _SCOPE_NAME.fullmatch(n) and n != token]
    return tuple(plain[:_MAX_SCOPE_NAMES])


def _state_of_current_token() -> _TokenState | None:
    """Return the recorded token state if it belongs to the token in effect now.

    Returns:
        The state, or None when there is none, no token is set, the token was
        replaced since, or the secrets file cannot be read (logged).
    """
    state = _token_state
    if state is None:
        return None
    try:
        token = secrets_store.get_secret(secrets_store.GITHUB_TOKEN)
    except OSError:
        logger.error("Could not read the secrets file to check the GitHub token")
        return None
    if token is None or _fingerprint(token) != state.fingerprint:
        return None
    return state


def github_token_warning() -> str | None:
    """Tell the owner when the GitHub token in use carries permissions.

    Only a classic token reports its scopes (`X-OAuth-Scopes`), and only after
    a request was made with it. A fine-grained token sends no such header, so
    its permissions cannot be seen and no warning is given for it.

    Returns:
        A warning naming the scopes of the token in effect now, or None when its
        last authenticated response showed none, none was seen yet, the token
        was replaced or cleared since, or no token is set.
    """
    state = _state_of_current_token()
    if state is None or state.rejected or not state.scopes:
        return None
    return (
        f"The GitHub token has permissions ({', '.join(state.scopes)}). Patch "
        "bundles can read it. Use a token with no permissions."
    )


def github_token_permissions() -> str | None:
    """Summarise what is known about the permissions of the token in effect now.

    Returns:
        None when no token is set (or the secrets file cannot be read, logged).
        Otherwise one of: "unchecked" (no successful authenticated request with
        this token yet, or GitHub rejected it), "none" (a classic token with no
        scopes), "present" (a classic token with scopes; see
        `github_token_warning` for the names) or "unverifiable" (GitHub sent no
        scope header: a fine-grained or GitHub App token, whose permissions are
        not reported this way).
    """
    try:
        token = secrets_store.get_secret(secrets_store.GITHUB_TOKEN)
    except OSError:
        logger.error("Could not read the secrets file to check the GitHub token")
        return None
    if token is None:
        return None
    state = _token_state
    if state is None or state.rejected or state.fingerprint != _fingerprint(token):
        return "unchecked"
    if state.scopes is None:
        return "unverifiable"
    return "present" if state.scopes else "none"


def github_token_rejected() -> str | None:
    """Tell the owner that GitHub rejected the token in effect now.

    Returns:
        The fixed rejection text (never the token) while the last authenticated
        request with the current token got a 401; None once the token is
        replaced or cleared or a later authenticated request succeeds.
    """
    state = _state_of_current_token()
    return TOKEN_REJECTED_MESSAGE if state is not None and state.rejected else None


def _get_with_token(url: str, token: str) -> requests.Response:
    """GET `url` on api.github.com with the token as a bearer header.

    The session ignores the environment (no netrc, which could replace the
    header), takes proxies and the CA bundle from it explicitly, and never lets
    `requests` follow a redirect: only a redirect that stays on
    https://api.github.com is followed, by this function, with the same header.

    Args:
        url: A URL that already passed `_is_api_url`.
        token: The GitHub token.

    Returns:
        The final response, never a 401 (that raises).

    Raises:
        GitHubApiError: The request failed or was redirected away. The message
            holds only the failure's class name.
        GitHubTokenRejectedError: GitHub answered 401.
    """
    failure: str | None = None
    response: requests.Response | None = None
    try:
        with requests.Session() as session:
            session.trust_env = False
            session.proxies.update(requests.utils.get_environ_proxies(url))
            verify = (
                os.environ.get("REQUESTS_CA_BUNDLE")
                or os.environ.get("CURL_CA_BUNDLE")
                or True
            )
            headers = {"Authorization": f"Bearer {token}"}
            target = url
            for _ in range(MAX_API_REDIRECTS + 1):
                response = session.get(
                    target,
                    headers=headers,
                    timeout=30,
                    verify=verify,
                    allow_redirects=False,
                )
                if not response.is_redirect:
                    break
                target = urljoin(target, response.headers["location"])
                if not _is_api_url(target):
                    failure = "RedirectedAway"
                    break
            else:
                failure = "TooManyRedirects"
    except (requests.exceptions.RequestException, ValueError) as error:
        # Raised below, outside this block: the original error text and the
        # exception chain can carry the request headers.
        failure = type(error).__name__
    if failure is not None or response is None:
        raise GitHubApiError(f"GitHub API request failed ({failure}).")
    if response.status_code == 401:
        raise GitHubTokenRejectedError()
    return response


def github_api_get(url: str) -> tuple[requests.Response, bool]:
    """GET a URL on the GitHub REST API, with the saved token when there is one.

    This is the only place the token is read for a request. It goes to
    https://api.github.com as a header and nowhere else: any other target is
    refused, redirects off api.github.com are not followed, and the token is
    never in the URL or a body. Without a token the request is a plain
    `requests.get`.

    Args:
        url: Absolute https://api.github.com/... URL, already built from a
            repository name that passed `is_valid_repo`.

    Returns:
        The response and whether the token was sent.

    Raises:
        GitHubHostError: `url` is not an https URL on api.github.com. Nothing
            was sent.
        GitHubApiError: The authenticated request failed (fixed text).
        GitHubTokenRejectedError: GitHub rejected the token with 401.
        requests.exceptions.RequestException: The unauthenticated request failed.
    """
    global _token_state
    if not _is_api_url(url):
        raise GitHubHostError("Only https://api.github.com can receive API calls.")
    try:
        token = secrets_store.get_secret(secrets_store.GITHUB_TOKEN)
    except OSError:
        logger.error("Could not read the GitHub token; calling GitHub without it")
        token = None
    if token is None:
        _token_state = None
        return _get_following(url), False
    fingerprint = _fingerprint(token)
    try:
        response = _get_with_token(url, token)
    except GitHubTokenRejectedError:
        _token_state = _TokenState(fingerprint, None, True)
        raise
    scopes = _parse_scopes(response.headers.get("X-OAuth-Scopes"), token)
    _token_state = _TokenState(fingerprint, scopes, False)
    return response, True


def _rate_limit_from_response(
    response: requests.Response, token_set: bool = True
) -> GitHubRateLimitError | None:
    """Builds a rate limit error if the response is a GitHub rate limit refusal.

    Args:
        response: Response from the GitHub API.
        token_set: Whether the request carried the GitHub token. When False the
            error says a token can be set in Settings.

    Returns:
        The error to raise, or None when the response is not a rate limit
        (for example a 403 on a private repository).
    """
    if response.status_code not in (403, 429):
        return None
    headers = response.headers
    try:
        if headers.get("retry-after") is not None:
            reset_epoch = time.time() + float(headers["retry-after"])
            return GitHubRateLimitError(reset_epoch, not token_set)
        if headers.get("x-ratelimit-remaining") == "0":
            reset = headers.get("x-ratelimit-reset")
            return GitHubRateLimitError(float(reset) if reset else None, not token_set)
    except ValueError:
        return GitHubRateLimitError(suggest_token=not token_set)
    return None


# Release `.json` assets named like a patch list: `patches.json`,
# `patches-list.json`, optionally with a plain version (`patches-1.2.3.json`).
# Other names (`patches-1.2.3.cdx.json`, `*.tooling.json`) are not tried first.
PATCH_LIST_ASSET_NAME = re.compile(
    r"patches(-list)?(-v?\d+(\.\d+)*([-+][0-9A-Za-z.]+)?)?\.json", re.IGNORECASE
)

# Largest oddly named `.json` asset tried before the repository list. Enforced on
# the bytes received, not on the size the release declares.
MAX_UNNAMED_LIST_BYTES = 256 * 1024

# Largest patch list accepted from a named asset or from the repository. Real
# lists hold a few hundred patches (tens to a few hundred KiB, and the biggest
# known is about 1 MiB), so 8 MiB leaves a wide margin and still bounds what is
# held in memory while `SYNC_LOCK` is held.
MAX_NAMED_LIST_BYTES = 8 * 1024 * 1024

# Largest oddly named `.json` asset tried at all, as the very last resort.
MAX_OVERSIZED_LIST_BYTES = 2 * 1024 * 1024

# Longest release tag or ref accepted in a URL.
MAX_REF_LENGTH = 200


class _TooLargeError(Exception):
    """A download passed its byte limit; the message says which one."""


def _download_bounded(url: str, path: Path, max_bytes: int) -> None:
    """Download a file, stopping as soon as it is larger than `max_bytes`.

    The declared size is not trusted: the bytes actually received are counted.

    Args:
        url: URL to fetch.
        path: File to write. It may be left partial; the caller removes it.
        max_bytes: Largest size accepted.

    Raises:
        _TooLargeError: The file is, or declares itself, larger than `max_bytes`.
        requests.exceptions.RequestException: The request or transfer failed.
        OSError: The file could not be written.
    """
    response = _get_following(url, stream=True)
    try:
        response.raise_for_status()
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise _TooLargeError(f"it declares {declared} bytes (limit {max_bytes})")
        received = 0
        with open(path, "wb") as file:
            for data in response.iter_content(64 * 1024):
                received += len(data)
                if received > max_bytes:
                    raise _TooLargeError(f"it is larger than {max_bytes} bytes")
                file.write(data)
    finally:
        response.close()


def _download_patch_list(url: str, output_path: Path, max_bytes: int) -> bool:
    """Download a file and keep it only when it is a real patch list.

    The data goes to a side file first, so an unreadable download never
    replaces what is at `output_path`.

    Args:
        url: URL to fetch.
        output_path: Final location of the patch list.
        max_bytes: The download is cut off beyond this many received bytes.

    Returns:
        True when the file was a patch list and now sits at `output_path`.
        False when the download failed, was too large, or the file is not a
        patch list; the reason is printed and `output_path` is untouched.
    """
    candidate_path = Path(f"{output_path}.candidate")
    try:
        _download_bounded(url, candidate_path, max_bytes)
        _read_patch_list(candidate_path)
        os.replace(candidate_path, output_path)
    except _PatchListFormatError as error:
        print(f"    [!] Not a patch list, ignored: {error}")
        return False
    except _TooLargeError as error:
        print(
            f"    [!] Refused {url.rsplit('/', 1)[-1]} for its size: {error}. "
            "It was not kept."
        )
        return False
    except (requests.exceptions.RequestException, OSError) as error:
        print(f"    [!] Failed to download {url.rsplit('/', 1)[-1]}: {error}")
        return False
    finally:
        candidate_path.unlink(missing_ok=True)
    return True


def _asset_size(asset: dict) -> int | None:
    """Return an asset's declared size in bytes, or None when it is not known.

    Args:
        asset: Asset object of a GitHub release.

    Returns:
        The `size` when it is a non-negative integer, else None.
    """
    size = asset.get("size")
    if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
        return size
    return None


def _unnamed_json_assets(assets: list[dict]) -> list[dict]:
    """Return the `.json` assets that are not named like a patch list.

    Args:
        assets: Asset objects of a GitHub release.

    Returns:
        The assets whose name ends in `.json` (any case) and does not match
        `PATCH_LIST_ASSET_NAME`.
    """
    return [
        a
        for a in assets
        if str(a.get("name", "")).lower().endswith(".json")
        and not PATCH_LIST_ASSET_NAME.fullmatch(str(a["name"]))
    ]


def _json_assets_to_try(assets: list[dict], named: bool) -> list[dict]:
    """Pick the release `.json` assets worth trying as the patch list.

    Args:
        assets: Asset objects of a GitHub release.
        named: True for assets named like a patch list, best name first
            (`patches-list.json`, `patches.json`, then versioned names). False
            for the other `.json` assets of known size up to
            `MAX_UNNAMED_LIST_BYTES`, smallest first.

    Returns:
        The assets to try, in order. Large unnamed files and unnamed files of
        unknown size are never included; see `_oversized_json_assets`.
    """
    if named:
        matching = [
            a
            for a in assets
            if str(a.get("name", "")).lower().endswith(".json")
            and PATCH_LIST_ASSET_NAME.fullmatch(str(a["name"]))
        ]
        return sorted(
            matching, key=lambda a: a["name"].lower() not in JSON_FILENAME_CANDIDATES
        )
    small = [
        a
        for a in _unnamed_json_assets(assets)
        if (size := _asset_size(a)) is not None and size <= MAX_UNNAMED_LIST_BYTES
    ]
    return sorted(small, key=lambda a: _asset_size(a) or 0)


def _oversized_json_assets(assets: list[dict]) -> list[dict]:
    """Pick the unnamed `.json` assets that only the last-resort pass may try.

    Args:
        assets: Asset objects of a GitHub release.

    Returns:
        Unnamed `.json` assets that are over `MAX_UNNAMED_LIST_BYTES` or of
        unknown size, smallest first, unknown sizes last.
    """
    large = [
        a
        for a in _unnamed_json_assets(assets)
        if (size := _asset_size(a)) is None or size > MAX_UNNAMED_LIST_BYTES
    ]
    return sorted(large, key=lambda a: (_asset_size(a) is None, _asset_size(a) or 0))


def _try_release_assets(
    assets: list[dict],
    tag: object,
    output_path: Path,
    max_bytes: int = MAX_NAMED_LIST_BYTES,
) -> bool:
    """Try release assets in order until one is a patch list.

    Args:
        assets: Candidate assets, best first.
        tag: Tag of the release the assets belong to.
        output_path: Final location of the patch list.
        max_bytes: Byte limit for each download.

    Returns:
        True when a patch list was written to `output_path`.
    """
    for asset in assets:
        url = _asset_url(asset, tag)
        if url is None:
            continue
        print(f"    -> Downloading {asset['name']}...")
        if _download_patch_list(url, output_path, max_bytes):
            return True
    return False


def _try_oversized_assets(assets: list[dict], tag: object, output_path: Path) -> bool:
    """Last resort: try large or unsized unnamed `.json` assets, bounded.

    Args:
        assets: Asset objects of a GitHub release.
        tag: Tag of the release.
        output_path: Final location of the patch list.

    Returns:
        True when a patch list was written to `output_path`.
    """
    candidates = []
    for asset in _oversized_json_assets(assets):
        size = _asset_size(asset)
        if size is not None and size > MAX_OVERSIZED_LIST_BYTES:
            print(
                f"    [!] Skipped {asset['name']}: {size} bytes is over the "
                f"{MAX_OVERSIZED_LIST_BYTES} byte limit for an unnamed list."
            )
            continue
        reason = "too large for the first pass" if size else "its size is unknown"
        print(f"    -> Last resort: {asset['name']} ({reason}), at most 2 MiB.")
        candidates.append(asset)
    return _try_release_assets(candidates, tag, output_path, MAX_OVERSIZED_LIST_BYTES)


def _download_release_patch_list(
    repo_path: str, release_data: dict, output_path: Path
) -> bool:
    """Get a source's patch list for a release, keeping only a real one.

    Order: release assets named like a patch list, the list file in the
    repository at the release tag, small other `.json` assets, then, as a last
    resort, large or unsized other `.json` assets (bounded download). A file
    that is not a patch list (for example an SBOM) is skipped, never kept.

    Args:
        repo_path: GitHub 'owner/repo'.
        release_data: GitHub release object.
        output_path: Final location of the patch list.

    Returns:
        True when a patch list was written to `output_path`.
    """
    assets = release_data.get("assets", [])
    tag = release_data.get("tag_name")
    if _try_release_assets(_json_assets_to_try(assets, True), tag, output_path):
        return True
    print(f"    -> No named patch list asset. Fetching it from source ({tag})...")
    if _try_download_json_candidates(repo_path, tag, output_path):
        return True
    small = _json_assets_to_try(assets, False)
    if _try_release_assets(small, tag, output_path, MAX_UNNAMED_LIST_BYTES):
        return True
    return _try_oversized_assets(assets, tag, output_path)


# A git ref safe to put in a raw.githubusercontent.com path: git also allows
# `%`, `#` and `?` in tag names, and percent-decoding can make `..` segments.
# One path segment; a tag may hold several, joined by `/` (`release/1.0`).
_REF_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}")


def _is_valid_ref(ref: object) -> bool:
    """Tell whether a release tag or branch is safe inside a URL path.

    Args:
        ref: Candidate tag taken from a GitHub response.

    Returns:
        True for at most `MAX_REF_LENGTH` characters made of `/`-separated
        segments, each 1-100 ASCII letters, digits, `.`, `_`, `+` or `-` that
        start with a letter or digit and hold no `..`. So no empty segment, no
        `.` or `..` segment, no `%`, `?`, `#`, backslash or space.
    """
    if not isinstance(ref, str) or len(ref) > MAX_REF_LENGTH:
        return False
    return all(
        _REF_PATTERN.fullmatch(segment) is not None and ".." not in segment
        for segment in ref.split("/")
    )


def _quote_ref(ref: str) -> str:
    """Quote a validated ref for a URL path, keeping `/` and `+` literal.

    Args:
        ref: A ref that passed `_is_valid_ref`.

    Returns:
        The ref as it is written in a raw file URL.
    """
    return quote(ref, safe="/+")


# Characters `quote` encodes but that a URL may carry literally, and that GitHub
# may leave as they are in an asset name. `+` is the one already seen in tags.
_LITERAL_NAME_CHARS = frozenset("+@,=()!*'$&;:")


def _name_matches(url_name: str, name: str) -> bool:
    """Tell whether a URL's last segment is exactly the asset name, escaped.

    Each character of the name may appear as `%XX` (uppercase, as `quote` writes
    it) and, for letters, digits, `-._~` and `_LITERAL_NAME_CHARS`, also as
    itself. Nothing is decoded: a `%` in the URL must start the escape of the
    name's own character, so `%2E`, `%25` for a literal `%`, `/`, `?`, `#` and
    backslash cannot stand for anything else.

    Args:
        url_name: The URL segment after the tag.
        name: The asset's `name` from the release.

    Returns:
        True when the segment spells `name` in one of those ways.
    """
    position = 0
    for char in name:
        escaped = quote(char, safe="")
        literal_ok = escaped == char or char in _LITERAL_NAME_CHARS
        if literal_ok and url_name.startswith(char, position):
            position += 1
        elif url_name.startswith(escaped, position):
            position += len(escaped)
        else:
            return False
    return position == len(url_name)


def _is_release_asset_url(url: object, tag: object, name: object) -> bool:
    """Tell whether a `browser_download_url` is a real GitHub release download.

    The path must be exactly `/<owner>/<repo>/releases/download/<tag>/<name>`
    built from the release's own tag and the asset's own name, with the slash
    inside a tag written either literally or as `%2F` (GitHub's form is not
    known for certain). Nothing else is accepted, so decoding tricks (`%2e%2e`,
    `%252F`), extra segments, `?`, `#` and backslashes all fail.

    Args:
        url: Value taken from a GitHub release response.
        tag: The release's `tag_name`.
        name: The asset's `name`.

    Returns:
        True only for https on exactly github.com (no user info, port, query or
        fragment), a valid owner/repo, a valid tag and a plain file name.
    """
    if not isinstance(url, str) or not url.isascii() or not url.isprintable():
        return False
    if " " in url or "\\" in url:
        return False
    if not isinstance(tag, str) or not _is_valid_ref(tag):
        return False
    if not isinstance(name, str) or name in {"", ".", ".."} or "/" in name:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or parts.netloc != "github.com":
        return False
    if parts.query or parts.fragment:
        return False
    segments = parts.path.split("/", 3)
    if len(segments) != 4 or segments[0] != "":
        return False
    if not is_valid_repo(f"{segments[1]}/{segments[2]}"):
        return False
    tag_forms = {quote(tag, safe=safe) for safe in ("/+", "+", "/", "")}
    allowed = {f"releases/download/{t}/" for t in tag_forms}
    prefix, _, url_name = segments[3].rpartition("/")
    return f"{prefix}/" in allowed and _name_matches(url_name, name)


def _asset_url(asset: dict, tag: object) -> str | None:
    """Return the asset's download URL when it is valid, else None (reported).

    Args:
        asset: Asset object of a GitHub release.
        tag: The release's `tag_name`.

    Returns:
        The `browser_download_url`, or None when it failed `_is_release_asset_url`.
    """
    url = asset.get("browser_download_url")
    if _is_release_asset_url(url, tag, asset.get("name")):
        return str(url)
    print(f"    [!] Refusing the download URL of {_quoted(asset.get('name'))}.")
    return None


def _try_download_json_candidates(
    repo_path: str, ref: object, output_path: Path | str
) -> bool:
    """Try the candidate list file names in a repo at a ref, via raw.githubusercontent.

    A file that is not a patch list is not kept. A repository or ref that is
    not valid is refused without a request.

    Args:
        repo_path: GitHub 'owner/repo'.
        ref: Release tag or branch name. It may hold `/` (`release/1.0`).
        output_path: Final location of the patch list.

    Returns:
        True on the first candidate that was a patch list and was written.
    """
    if not is_valid_repo(repo_path) or not isinstance(ref, str):
        print(f"    [!] Not fetching from source: unusable ref {_quoted(ref)}.")
        return False
    if not _is_valid_ref(ref):
        print(f"    [!] Not fetching from source: unusable ref {_quoted(ref)}.")
        return False
    for filename in JSON_FILENAME_CANDIDATES:
        raw_url = (
            f"https://raw.githubusercontent.com/{repo_path}/{_quote_ref(ref)}/"
            f"{filename}"
        )
        if _download_patch_list(raw_url, Path(output_path), MAX_NAMED_LIST_BYTES):
            return True
    return False


STILL_UNREADABLE = (
    "    [!] The patch list of {repo} is still unreadable after downloading it "
    "again. Not retrying until the next sync."
)


def _patch_list_is_unreadable(repo_path: str, extensions_to_paths: dict) -> bool:
    """Tell whether a source's patch list is on disk but fails to load.

    Args:
        repo_path: GitHub 'owner/repo'.
        extensions_to_paths: Map of wanted asset extension to local file path.

    Returns:
        True when the `.json` entry is the source's own patch list file, the file
        is present and `load_patch_list` rejects it. A missing file, a sync
        without a `.json` entry or a `.json` at another path is not "unreadable".
    """
    json_path = extensions_to_paths.get(".json")
    if json_path is None or json_path != get_source_paths(repo_path)[".json"]:
        return False
    if not json_path.exists():
        return False
    try:
        load_patch_list(repo_path)
    except PatchListError:
        return True
    return False


def _download_release_asset(release_data: dict, ext: str, output_path: Path) -> bool:
    """Download the release asset whose name ends in `ext`.

    Args:
        release_data: GitHub release object.
        ext: Wanted name suffix, for example `.mpp`.
        output_path: Where to write the file.

    Returns:
        True when the file was downloaded; False when there is no such asset,
        its URL was refused or the download failed (reported).
    """
    tag = release_data.get("tag_name")
    for asset in release_data.get("assets", []):
        if not asset["name"].endswith(ext):
            continue
        url = _asset_url(asset, tag)
        if url is None:
            return False
        print(f"    -> Downloading {asset['name']}...")
        try:
            download_with_progress(url, output_path)
        except (requests.exceptions.RequestException, OSError) as error:
            print(f"    [!] Failed to download {asset['name']}: {error}")
            return False
        return True
    return False


def check_and_download_release(
    repo_path: str, extensions_to_paths: dict[str, Path], force: bool = False
) -> bool:
    """Syncs a repo's latest release assets into the given local paths.

    Files are downloaded next to their final path first and moved into place
    only when every wanted file arrived, so a bundle and a list on disk always
    belong to the same tag. When a new release has no patch list, nothing is
    replaced and the old tag stays recorded, so the next sync tries again. The
    exception is a stored list that is already unreadable, or an unchanged
    tag: the stored list is then kept as it is.

    A patch list that is on disk but fails to load is downloaded again, even at
    an unchanged tag and inside the update-check throttle, and only that file is
    fetched. One attempt is made per call; a list that is still unreadable
    afterwards is reported on stdout, nothing is recorded, and the next sync
    tries again.

    Args:
        repo_path: GitHub 'owner/repo'.
        extensions_to_paths: Map of wanted asset extension to local file path.
        force: Skip the update-check throttle.

    Returns:
        True when files were downloaded and the tag was recorded; False on
        failure, when already up to date, when a repair fetched a list that
        is still unreadable, or when `repo_path` is not a valid `owner/name`
        (logged, and no request is made).

    Raises:
        GitHubRateLimitError: GitHub refused the release lookup due to a rate limit.
        GitHubTokenRejectedError: GitHub rejected the saved token (HTTP 401). The
            lookup is not retried without it.
    """
    if not is_valid_repo(repo_path):
        # Checked here, where every GitHub URL is built, so no caller can send
        # a request for a name that is not a real owner/name.
        logger.error("Skipping sync of invalid repository %r", str(repo_path)[:100])
        return False
    api_url = f"https://api.github.com/repos/{repo_path}/releases/latest"
    local_versions = load_local_versions()
    current_tag = local_versions.get(repo_path)

    # Safeguard: Check if any required files are missing locally
    missing_files = [path for path in extensions_to_paths.values() if not path.exists()]
    list_unreadable = _patch_list_is_unreadable(repo_path, extensions_to_paths)

    # Throttle: skip the API call entirely if we checked recently and have files
    last_check = local_versions.get(f"{repo_path}#last_check")
    # A move of new files into place that did not finish: fetch again, whatever
    # the throttle says, so a bundle and a list of different tags are not kept.
    incomplete_key = f"{repo_path}#incomplete"
    incomplete = incomplete_key in local_versions
    if (
        not force
        and not incomplete
        and not missing_files
        and not list_unreadable
        and last_check
        and (time.time() - last_check) < UPDATE_CHECK_INTERVAL
    ):
        return False

    release_data = None
    try:
        response, token_used = github_api_get(api_url)
        if response.status_code == 404:
            # Repo has no releases: fall back to default-branch raw files for .json
            print(f"[!] {repo_path} has no releases. Falling back to default branch...")
            if ".json" in extensions_to_paths:
                for branch in ("main", "master"):
                    if _try_download_json_candidates(
                        repo_path, branch, extensions_to_paths[".json"]
                    ):
                        if list_unreadable and _patch_list_is_unreadable(
                            repo_path, extensions_to_paths
                        ):
                            print(STILL_UNREADABLE.format(repo=repo_path))
                            return False
                        local_versions[repo_path] = "default-branch"
                        local_versions[f"{repo_path}#last_check"] = time.time()
                        save_local_versions(local_versions)
                        return True
            return False
        rate_limit = _rate_limit_from_response(response, token_used)
        if rate_limit is not None:
            raise rate_limit
        response.raise_for_status()
        release_data = response.json()
    except requests.exceptions.RequestException as e:
        print(f"[x] Failed to fetch updates for {repo_path}: {e}")
        return False

    latest_tag = release_data.get("tag_name")

    if (
        current_tag == latest_tag
        and not incomplete
        and not missing_files
        and not list_unreadable
    ):
        print(f"[\u2713] {repo_path} is up to date ({current_tag}).")
        local_versions[f"{repo_path}#last_check"] = time.time()
        save_local_versions(local_versions)
        return False

    repair_only = False
    if current_tag != latest_tag:
        print(f"[!] Update found for {repo_path}: {current_tag} -> {latest_tag}")
    elif incomplete or missing_files:
        print(f"[!] Local files for {repo_path} are incomplete. Re-downloading...")
    else:
        print(f"[!] The patch list of {repo_path} is unreadable. Re-downloading it...")
        extensions_to_paths = {".json": extensions_to_paths[".json"]}
        repair_only = True

    keep_stored_list = (current_tag == latest_tag and not incomplete) or list_unreadable
    # The list first: a release without a usable list must not cost a bundle
    # download that is thrown away on every sync.
    ordered = sorted(extensions_to_paths.items(), key=lambda item: item[0] != ".json")
    staged: dict[Path, Path] = {}
    try:
        for ext, output_path in ordered:
            staged_path = Path(f"{output_path}.staged")
            staged_path.unlink(missing_ok=True)
            if ext == ".json":
                found = _download_release_patch_list(
                    repo_path, release_data, staged_path
                )
            else:
                found = _download_release_asset(release_data, ext, staged_path)
            if found:
                staged[staged_path] = output_path
                continue
            if ext == ".json" and keep_stored_list and output_path.exists():
                # Nothing to pair with a new bundle that is worse than this
                # list; a list that stays unreadable is reported below.
                print(f"    [!] No readable patch list for {repo_path} in {latest_tag}")
                continue
            print(f"    [!] Could not obtain {ext} for {repo_path}.")
            print("        Not marking as synced.")
            return False
        if staged:
            # Written before the first move and removed after the last: if the
            # process dies or a move fails in between, the next sync starts over.
            local_versions[incomplete_key] = latest_tag
            save_local_versions(local_versions)
            for staged_path, output_path in staged.items():
                os.replace(staged_path, output_path)
            del local_versions[incomplete_key]
            save_local_versions(local_versions)
    except OSError as error:
        logger.error("Could not move the files of %s into place: %s", repo_path, error)
        return False
    finally:
        for staged_path in staged:
            staged_path.unlink(missing_ok=True)

    still_unreadable = _patch_list_is_unreadable(repo_path, extensions_to_paths)
    if still_unreadable:
        print(STILL_UNREADABLE.format(repo=repo_path))
        if repair_only:
            # Nothing usable arrived: callers must not treat this as an update.
            return False

    local_versions[repo_path] = latest_tag
    local_versions[f"{repo_path}#last_check"] = time.time()
    save_local_versions(local_versions)
    return True


# GitHub owner: 1-39 ASCII letters, digits or hyphens, no hyphen at either end.
# Repository name: 1-100 ASCII letters, digits, ".", "_" or "-". Python's \w and
# $ are not used: \w matches non-ASCII letters and $ matches before a newline.
REPO_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}"
)


def is_valid_repo(repo: object) -> bool:
    """Tell whether `repo` is a real GitHub `owner/name`, safe inside a URL.

    Args:
        repo: Candidate value, for example from the config file.

    Returns:
        True for an ASCII `owner/name` that follows GitHub's naming rules. False
        for anything else: dot-only names such as `.` and `..` (they would move
        the request to another API path), a `.git` suffix, extra path segments,
        whitespace, control characters, non-ASCII letters and non-strings.

    Example:
        is_valid_repo("MorpheApp/morphe-patches") -> True
        is_valid_repo("owner/..") -> False
    """
    if not isinstance(repo, str) or REPO_PATTERN.fullmatch(repo) is None:
        return False
    name = repo.split("/", 1)[1]
    return set(name) != {"."} and not name.lower().endswith(".git")


def normalize_repo(raw):
    """Reduces a pasted GitHub reference to 'owner/repo', or None if invalid.

    Accepts URLs (with or without scheme/www), a trailing '.git' and extra
    path segments such as '/tree/main' or '/releases'.
    """
    text = re.sub(r"^(https?://)?(www\.)?github\.com/", "", raw.strip(), flags=re.I)
    parts = [part for part in text.split("/") if part]
    if len(parts) < 2:
        return None
    repo = "/".join(parts[:2]).removesuffix(".git")
    return repo if is_valid_repo(repo) else None


def _quoted(value: object, limit: int = 60) -> str:
    """Return a short, single-line, quoted form of any config value for messages.

    Args:
        value: Any value, for example from the config file or a GitHub response.
        limit: Most characters of the text kept.

    Returns:
        The `repr` of the first `limit` characters, so it holds no line break.
    """
    text = value if isinstance(value, str) else repr(value)
    return repr(text[:limit])


def find_file_collision(repo: str, sources: list[dict]) -> str | None:
    """Find an existing source whose local files `repo` would share.

    Two different repos can name the same files (`/` becomes `-`, case is
    folded), and syncing one would then replace the other's bundle.

    Args:
        repo: A valid `owner/name`.
        sources: The `sources` list of the config; entries with a missing,
            non-text or invalid repo are ignored.

    Returns:
        The repo of the first source with the same files, or None.
    """
    files = get_source_paths(repo)[".mpp"]
    for source in sources:
        other = source.get("repo")
        if is_valid_repo(other) and get_source_paths(other)[".mpp"] == files:
            return str(other)
    return None


def _reject_source_path_alias(repo: str, sources: list[dict]) -> None:
    """Reject an active, different repo that shares this repo's local files."""
    other = source_path_alias(repo, sources)
    if other is not None:
        raise PatchSelectionError("Selected patch source is not usable")


def source_path_alias(repo: str, sources: list[dict]) -> str | None:
    """Find another active configured repo that maps to this source's files."""
    local_bundle = get_source_paths(repo)[".mpp"]
    for source in sources:
        other = source.get("repo")
        if (
            source.get("active", True)
            and is_valid_repo(other)
            and isinstance(other, str)
            and other.casefold() != repo.casefold()
            and get_source_paths(other)[".mpp"] == local_bundle
        ):
            return other
    return None


def plan_source_sync(config: dict) -> tuple[list[dict], list[str]]:
    """Decide which active sources can be synced and describe the ones that cannot.

    A source is skipped when its repo is not a valid `owner/name` (a hand-edited
    config) or when its local files are already taken by an earlier active
    source: the first in config order keeps them.

    Args:
        config: The loaded config.

    Returns:
        The sources to sync, and one message per skipped source or tool
        repository (naming it, never a secret). Nothing is requested here.
    """
    usable: list[dict] = []
    problems: list[str] = []
    owners: dict[Path, str] = {}
    for key, repo in (config.get("repositories") or {}).items():
        if key in ("cli", "apkeditor") and not is_valid_repo(repo):
            problems.append(f"Tool repository {key!r} is invalid: {_quoted(repo)}")
    for source in config.get("sources", []):
        if not source.get("active", True):
            continue
        label = _quoted(source.get("name"))
        repo = source.get("repo")
        if not is_valid_repo(repo):
            problems.append(f"Source {label} has an invalid repository {_quoted(repo)}")
            continue
        files = get_source_paths(repo)[".mpp"]
        if files in owners:
            problems.append(
                f"Source {label} ({repo}) uses the same local files as source "
                f"{owners[files]} and is skipped"
            )
            continue
        owners[files] = f"{label} ({repo})"
        usable.append(source)
    return usable, problems


def get_source_paths(repo_path):
    """Generates consistent local paths for a source's MPP and JSON files."""
    safe_name = repo_path.replace("/", "-").lower()
    return {
        ".mpp": DATA_DIR / "bin" / f"patches-{safe_name}.mpp",
        ".json": DATA_DIR / "bin" / f"patches-{safe_name}.json",
    }


def check_dependencies(config):
    """Checks if required system tools are available."""
    results = {"java": False, "scraper": False}
    java_bin = config.get("settings", {}).get("java_path", "java")

    try:
        run_child([java_bin, "-version"], capture_output=True, check=True)
        results["java"] = True
    except:
        pass

    try:
        from src.apkmirror_downloader import ApkMirror_Downloader

        results["scraper"] = True
    except ImportError:
        pass

    return results


def _apply_versions(config: dict, local_versions: dict) -> None:
    """Write installed source and tool versions into `config` in place.

    Args:
        config: Config dict to update.
        local_versions: Installed version per repo, from `load_local_versions`.
    """
    for source in config.get("sources", []):
        repo = source.get("repo")
        version_tag = local_versions.get(repo) if isinstance(repo, str) else None
        if version_tag:
            source["version"] = version_tag

    tools = config.setdefault("tools", {})
    repos = config.get("repositories", {})
    if repos.get("cli") in local_versions:
        tools["cli_version"] = local_versions[repos["cli"]]
    if repos.get("apkeditor") in local_versions:
        tools["apkeditor_version"] = local_versions[repos["apkeditor"]]


def selected_patches(app: dict, config: dict | None = None) -> dict:
    """Return an app's patch selection in a stable, order-independent form.

    Args:
        app: App entry from the config.

    Returns:
        Sorted legacy names or source-keyed names under "include", plus sorted
        excluded names. Mapping repo keys are canonicalized against config when
        available by callers before storing build records.
    """
    included = app.get("include_patches") or []
    if isinstance(included, dict):
        configured_repos: dict[str, str] = {}
        if config is not None:
            for source in config.get("sources", []):
                repo = source.get("repo")
                if not is_valid_repo(repo):
                    continue
                key = repo.casefold()
                if key in configured_repos:
                    raise PatchSelectionError("Duplicate source repositories in config")
                configured_repos[key] = repo
        normalized: dict[str, list[str]] = {}
        for repo, names in included.items():
            if not is_valid_repo(repo) or not isinstance(names, list):
                raise PatchSelectionError("Invalid source-keyed patch selection")
            key = configured_repos.get(repo.casefold(), repo.casefold())
            if key in normalized:
                raise PatchSelectionError("Duplicate source keys in patch selection")
            if not all(isinstance(name, str) for name in names):
                raise PatchSelectionError("Patch selection names must be text")
            normalized[key] = sorted(
                set(names), key=lambda name: (name.casefold(), name)
            )
        included = dict(sorted(normalized.items()))
    elif isinstance(included, list):
        included = sorted(included)
    else:
        raise PatchSelectionError("Patch selection must be a list or source mapping")

    return {
        "include": included,
        "exclude": sorted(app.get("exclude_patches") or []),
    }


def effective_build_selection(app: dict, config: dict | None) -> dict:
    """Selection identity including artifact requirements affecting its bytes."""
    selection = selected_patches(app, config)
    minimum_sdk = required_minimum_sdk(app, config or {})
    if minimum_sdk is not None:
        selection = {"patches": selection, "minimum_sdk": minimum_sdk}
    return selection


# Version of how patched APKs are put together. It is part of the version hash
# and of every build record. Bump it whenever a change to `patch_app` means
# APKs built before it are wrong or incomplete (for example selectors that
# were grouped under the wrong bundle): every app is then rebuilt once and
# gets a new version string. Do not bump it for ordinary changes.
BUILD_LAYOUT = 2


def build_combined_version(
    app_version: str,
    patch_versions: dict[str, str],
    selection: dict,
) -> str:
    """Build the version string Obtainium sees for one patched APK.

    The string is `<app version>-<primary patch tag>-<hash>`. The primary tag is
    the version of the Morphe base source if it was used, else of the first
    used source. It becomes part of the APK file name, so every character
    outside letters, digits and `._+-` (a `/` in `release/1.0`) becomes `-`.
    The hash covers every used source version and the patch selection, so it
    changes whenever one of them does and never otherwise.

    Args:
        app_version: Version of the unpatched app.
        patch_versions: Version per source repo, for the repos actually used.
        selection: Patch selection as returned by `selected_patches`.

    Returns:
        The combined version string, stable across runs and processes.

    Example:
        build_combined_version("1.0", {"own/src": "v2"}, selected_patches(app))
        -> "1.0-v2-3fa9c1d2"
    """
    primary_tag = "v0.0.0"
    if patch_versions:
        morphe_repo = next((r for r in patch_versions if is_base_repo(r)), None)
        primary_tag = patch_versions[morphe_repo or next(iter(patch_versions))]
    canonical_selection = dict(selection)
    included = canonical_selection.get("include", [])
    if isinstance(included, dict):
        canonical_selection["include"] = {
            repo: sorted(names, key=lambda name: (name.casefold(), name))
            for repo, names in sorted(included.items(), key=lambda item: item[0])
        }
    else:
        canonical_selection["include"] = sorted(included)
    canonical_selection["exclude"] = sorted(canonical_selection.get("exclude", []))
    digest_input = json.dumps(
        {
            "layout": BUILD_LAYOUT,
            "patch_versions": patch_versions,
            "patches": canonical_selection,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(digest_input.encode()).hexdigest()[:8]
    file_safe_tag = re.sub(r"[^A-Za-z0-9._+-]", "-", str(primary_tag))
    return f"{app_version}-{file_safe_tag}-{digest}"


def is_build_current(
    last_build: dict,
    app_version: str,
    app: dict,
    patch_versions: dict[str, str],
    patched_dir: Path,
    config: dict | None = None,
) -> bool:
    """Decide from the recorded inputs whether an app's APK needs no rebuild.

    Args:
        last_build: The app's `last_successful_build`, possibly empty.
        app_version: Version of the unpatched app about to be built.
        app: App entry from the config.
        patch_versions: Current version per active source repo.
        patched_dir: Directory holding the served APKs.

    Returns:
        True when the recorded build used the current build layout, the same app
        version, selection and source versions, and its APK is still on disk.
        Records written before `BUILD_LAYOUT` existed never match.
    """
    filename = last_build.get("filename")
    recorded_sources = last_build.get("patch_versions")
    if not filename or not recorded_sources or not (patched_dir / filename).is_file():
        return False
    return (
        last_build.get("build_layout") == BUILD_LAYOUT
        and last_build.get("app_version") == app_version
        and last_build.get("patches") == effective_build_selection(app, config)
        and all(patch_versions.get(r) == v for r, v in recorded_sources.items())
        and active_selected_repos(app, config).issubset(
            {repo.casefold() for repo in recorded_sources}
        )
    )


def active_selected_repos(app: dict, config: dict | None) -> set[str]:
    """Return active configured repos named by a source-keyed selection."""
    included = app.get("include_patches")
    if not isinstance(included, dict) or config is None:
        return set()
    selected = {repo.casefold() for repo, names in included.items() if names}
    return {
        source["repo"].casefold()
        for source in config.get("sources", [])
        if source.get("active", True)
        and is_valid_repo(source.get("repo"))
        and source["repo"].casefold() in selected
    }


def save_app_result(app_id: str, **fields: object) -> None:
    """Merge pipeline-owned fields into one app of the current config.

    Only `fields` are written, onto the file as it is now, so UI edits made during
    a build survive. If the app was deleted meanwhile, nothing is written.

    Args:
        app_id: Package id of the app the result belongs to.
        **fields: Result fields: status, error, last_successful_build. A text
            `error` is stored with secret values removed.
    """
    if isinstance(fields.get("error"), str):
        fields["error"] = secrets_store.redact(str(fields["error"]))

    def apply(config: dict) -> None:
        for app in config.get("apps", []):
            if app["id"] == app_id:
                app.update(fields)
                return

    update_config(apply)


# Held for the duration of every tool or source sync (the background sync, the
# build's own sync, and adding a source), so two syncs never write the same files
# or spend the GitHub rate limit at once. Never held together with the config
# lock across a network call: `update_config` is only called after the downloads.
SYNC_LOCK = threading.Lock()


def update_tools(config, quiet=False, force=False):
    """Syncs tool jars and active patch sources from GitHub, one sync at a time.

    Waits for a sync that is already running (for example the build's sync waits
    for a requested one) rather than skipping, so a build never goes ahead with
    half-written files.

    Args:
        config: App config to read repos and sources from; versions are written
            back into it and, if anything was downloaded, into the config file
            (only the version fields, on top of the file's current contents).
        quiet: Suppress progress output.
        force: Skip the update-check throttle.

    Returns:
        True if any file was downloaded.

    Raises:
        GitHubRateLimitError: GitHub rate limited the sync. Repos checked before
            the limit are still saved; the rest were skipped.
        GitHubTokenRejectedError: GitHub rejected the saved token. Every later
            call would fail the same way, so the rest were skipped (as above).
    """
    with SYNC_LOCK:
        return _update_tools_locked(config, quiet, force)


# Temp names a download leaves behind when the process dies mid-way.
_STALE_PATTERNS = ("*.part", "*.staged", "*.candidate")


def remove_stale_download_files() -> int:
    """Delete download leftovers in the bin directory.

    Only call it with `SYNC_LOCK` held: every download writes its temp file
    under that lock, so nothing here can belong to a download in flight.

    Returns:
        How many files were removed.
    """
    removed = 0
    for pattern in _STALE_PATTERNS:
        for stale in (DATA_DIR / "bin").glob(pattern):
            try:
                stale.unlink()
            except OSError as error:
                logger.error("Could not remove %s: %s", stale.name, error)
                continue
            removed += 1
    return removed


def _update_tools_locked(config: dict, quiet: bool, force: bool) -> bool:
    """Run one sync; `SYNC_LOCK` is held by the caller.

    Args:
        config: See `update_tools`.
        quiet: See `update_tools`.
        force: See `update_tools`.

    Returns:
        True if any file was downloaded.

    Raises:
        GitHubRateLimitError: See `update_tools`.
        GitHubTokenRejectedError: See `update_tools`.
    """
    if not quiet:
        print("--- Checking for Tool Updates ---")
    bin_dir = DATA_DIR / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    remove_stale_download_files()

    repos = config["repositories"]
    tools = config["tools"]

    any_updated = False
    stop_error: GitHubRateLimitError | GitHubTokenRejectedError | None = None

    try:
        if check_and_download_release(
            repos["cli"], {".jar": DATA_DIR / tools["cli_jar"]}, force=force
        ):
            any_updated = True

        # Download APKEditor
        if "apkeditor" in repos and "apkeditor_jar" in tools:
            if check_and_download_release(
                repos["apkeditor"],
                {".jar": DATA_DIR / tools["apkeditor_jar"]},
                force=force,
            ):
                any_updated = True

        # Update each source that has a usable repository of its own
        usable_sources, problems = plan_source_sync(config)
        for problem in problems:
            logger.error("Not syncing: %s", problem)
        for source in usable_sources:
            repo = source["repo"]
            if not quiet:
                print(
                    f"Checking for updates for source: {source.get('name')} ({repo})..."
                )
            if check_and_download_release(repo, get_source_paths(repo), force=force):
                any_updated = True
    except (GitHubRateLimitError, GitHubTokenRejectedError) as e:
        # Every remaining repo would hit the same limit or the same rejected
        # token: stop and report below, once.
        stop_error = e

    # SYNC VERSIONS BACK TO CONFIG
    local_versions = load_local_versions()
    _apply_versions(config, local_versions)
    if any_updated:
        # Persist onto the file as it is now: `config` may be a stale snapshot.
        update_config(lambda current: _apply_versions(current, local_versions))

    if not quiet:
        print("---------------------------------\n")
    if stop_error is not None:
        raise stop_error
    return any_updated


BASE_PATCHES_REPO = "MorpheApp/morphe-patches"
HUSHFACEBOOK_REPO = "SysAdminDoc/HushFacebook"
HUSHFACEBOOK_PACKAGE = "com.facebook.katana"
HUSHFACEBOOK_MINIMUM_SDK = 30


def is_base_repo(repo: str) -> bool:
    """Tells whether a source repo is the Morphe base patch bundle.

    Compared by repo (case-insensitive, exact) rather than display name, so a
    source added through the UI as "MorpheApp Patches" is recognised too. A fork
    such as "someone/morphe-patches" is not the base source.

    Args:
        repo: GitHub 'owner/repo' of a patch source.

    Returns:
        True for the Morphe base repo.
    """
    return repo.lower() == BASE_PATCHES_REPO.lower()


class VersionLookupError(Exception):
    """The APKMirror lookup of the newest version failed (not "found nothing")."""


class PatchListError(Exception):
    """A source's patch list file exists but cannot be used.

    Attributes:
        repo: The source repo whose patch list is unreadable.
    """

    def __init__(self, repo: str, reason: str) -> None:
        self.repo = repo
        file_name = get_source_paths(repo)[".json"].name
        super().__init__(
            f"The patch list of {repo} is unreadable ({reason}). Sync the sources "
            "to download it again. If this message stays, the list the source "
            f"publishes is broken, or the app cannot read or replace {file_name} in "
            "its bin folder (check the file's permissions and owner)."
        )


class _PatchListFormatError(Exception):
    """A file is not a usable patch list; the message gives the reason."""


def _read_patch_list(json_path: Path) -> list[dict[str, object]]:
    """Read and validate a patch list file.

    Accepts a top-level list of patches or an object with a `patches` list. At
    least one patch must have a text `name`; an empty list is not a patch list.

    Args:
        json_path: File to read.

    Returns:
        The patches, every one a JSON object.

    Raises:
        _PatchListFormatError: The file cannot be read, is not valid JSON, or
            does not have the shape of a patch list.
    """
    try:
        with open(json_path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except json.JSONDecodeError as error:
        raise _PatchListFormatError(f"invalid JSON: {error}") from error
    except UnicodeDecodeError as error:
        raise _PatchListFormatError("not valid UTF-8 text") from error
    except OSError as error:
        raise _PatchListFormatError(error.strerror or type(error).__name__) from error
    patches = data.get("patches") if isinstance(data, dict) else data
    if not isinstance(patches, list):
        raise _PatchListFormatError("no list of patches")
    if not all(isinstance(patch, dict) for patch in patches):
        raise _PatchListFormatError("an entry is not a JSON object")
    if not all(isinstance(p.get("name"), str | None) for p in patches):
        raise _PatchListFormatError("a patch name is not text")
    if not any(isinstance(patch.get("name"), str) for patch in patches):
        raise _PatchListFormatError("no patch has a name")
    return patches


def load_patch_list(repo: str) -> list[dict[str, object]]:
    """Read and validate the downloaded patch list of one source.

    Accepts a top-level list of patches or an object with a `patches` list. Callers
    check that the file exists first; a missing file means "not synced yet".

    Args:
        repo: Source repo, `owner/repo`.

    Returns:
        The patches, every one a JSON object.

    Raises:
        PatchListError: The file cannot be read, is not valid JSON, or does not
            have the shape of a patch list.
    """
    try:
        return _read_patch_list(get_source_paths(repo)[".json"])
    except _PatchListFormatError as error:
        raise PatchListError(repo, str(error)) from error


def get_supported_versions(
    package_name,
    config,
    scraper=None,
    force_refresh=False,
    quiet=False,
    raise_lookup_errors=False,
):
    """
    Fetches available versions for a package with prioritization:
    1. Pinned version in config (Return immediately)
    2. Newest Recommended version from patch sources (Filtered by selected patches)
    3. Newest version from APKMirror search (Fallback)

    Args:
        package_name: Android package id.
        config: The loaded config (apps and sources).
        scraper: Optional injected scraper; one is created and closed if None.
        force_refresh: Ignore the cached APKMirror version.
        quiet: Suppress progress output.
        raise_lookup_errors: Raise `VersionLookupError` when the APKMirror lookup
            fails instead of falling back to the cached versions.

    Returns:
        (final_list, None, None)

    Raises:
        PatchListError: An active source's patch list exists but cannot be read.
        VersionLookupError: The lookup failed and `raise_lookup_errors` is set.
    """
    app_config = next(
        (a for a in config.get("apps", []) if a["id"] == package_name), {}
    )
    raw_selection = app_config.get("include_patches") or []
    pinned_v = app_config.get("version")
    has_source_mapping = isinstance(raw_selection, dict)
    has_selected_patches = (
        any(raw_selection.values())
        if isinstance(raw_selection, dict)
        else bool(raw_selection)
    )
    if pinned_v and not has_source_mapping:
        if not quiet:
            print(
                f"    [+] Using pinned version from config for {package_name}: {pinned_v}"
            )
        return [str(pinned_v)], None, None

    selected_repos = (
        set(resolve_patch_selection(app_config, config, require_all=has_source_mapping))
        if has_selected_patches
        else set()
    )
    if has_source_mapping and has_selected_patches and not selected_repos:
        raise PatchSelectionError(
            f"{package_name}: none of the selected patch sources are active"
        )
    selected_repo_keys = {repo.casefold() for repo in selected_repos}

    if pinned_v:
        if not quiet:
            print(
                f"    [+] Using pinned version from config for {package_name}: {pinned_v}"
            )
        return [str(pinned_v)], None, None

    # 1. Parse Recommended versions from active patch sources
    # POLICY: Only look at sources that are either "Morphe Patches" (base)
    # OR sources that actually provide one of the selected patches.
    versions_by_source = {}  # repo -> set of versions
    providing_sources = []  # repos in config order

    for source in config.get("sources", []):
        if not source.get("active", True):
            continue
        repo = source["repo"]
        if has_selected_patches and repo.casefold() not in selected_repo_keys:
            continue
        json_path = get_source_paths(source["repo"])[".json"]
        if not json_path.exists():
            continue

        patches = load_patch_list(repo)

        s_versions = set()
        has_selected_patch = False

        for p in patches:
            if has_selected_patches and repo.casefold() in selected_repo_keys:
                has_selected_patch = True

            compat = p.get("compatiblePackages")
            if not compat:
                continue

            v_list = []
            if isinstance(compat, dict) and package_name in compat:
                v_list = compat[package_name]
            elif isinstance(compat, list):
                for pkg in compat:
                    if not isinstance(pkg, dict):
                        continue
                    pkg_id = pkg.get("packageName") or pkg.get("name")
                    if pkg_id != package_name:
                        continue
                    v_list = pkg.get("versions") or []
                    targets = pkg.get("targets")
                    if targets and isinstance(targets, list):
                        v_list = [
                            t.get("version")
                            for t in targets
                            if isinstance(t, dict)
                            and t.get("version")
                            and not t.get("isExperimental", False)
                        ]

            if v_list:
                if isinstance(v_list, list):
                    s_versions.update([str(v).strip() for v in v_list if v])
                elif isinstance(v_list, str):
                    s_versions.add(v_list.strip())

        if s_versions:
            versions_by_source[repo] = s_versions
            if has_selected_patch:
                providing_sources.append(repo)

    # Selected patches constrain providers; discovery with no selection retains
    # the existing behavior of merging recommendations across active sources.
    recommended_set = set()
    if has_selected_patches and providing_sources:
        # Collect versions from providing sources
        for repo in providing_sources:
            if repo in versions_by_source:
                # If we haven't picked a version yet, take the first provider's
                if not recommended_set:
                    recommended_set = versions_by_source[repo]
                else:
                    # INTERSECT versions if multiple sources provide patches
                    # (Ensure we pick a version compatible with ALL selected sources)
                    intersect = recommended_set.intersection(versions_by_source[repo])
                    if intersect:
                        recommended_set = intersect
                    else:
                        # If no intersection, fallback to the provider that isn't Morphe
                        if not is_base_repo(repo):
                            recommended_set = versions_by_source[repo]
    elif not has_selected_patches:
        for source_versions in versions_by_source.values():
            recommended_set.update(source_versions)

    recommended = sorted(
        list(recommended_set), key=_safe_version_sort_key, reverse=True
    )
    if recommended:
        if not quiet:
            print(
                f"    [+] Found {len(recommended)} compatible recommended versions. Using newest: {recommended[0]}"
            )
        return [recommended[0]], None, None

    # 2. Fallback to newest from APKMirror
    cache = load_version_cache()
    entry = cache.get(package_name, {})
    now = time.time()
    cached_remote = entry.get("versions", [])

    if not force_refresh and entry and (now - entry.get("timestamp", 0) < 43200):
        if cached_remote:
            return cached_remote, None, None

    if not quiet:
        print(f"Fetching newest version for {package_name} from APKMirror...")
    close_scraper = False
    if scraper is None:
        scraper = ApkMirror_Downloader()
        close_scraper = True

    try:
        newest = scraper.latest_version(package_name)
        if newest:
            versions = [newest]
            cache[package_name] = {"versions": versions, "timestamp": now}
            save_version_cache(cache)
            if not quiet:
                print(f"    [+] Found newest on APKMirror: {newest}")
            return versions, None, None
        return cached_remote, None, None
    except Exception as e:
        if raise_lookup_errors:
            raise VersionLookupError(f"{type(e).__name__}: {e}") from e
        if not quiet:
            print(f"    [!] Scraper failed for {package_name}: {e}")
        return cached_remote, None, None
    finally:
        if close_scraper:
            scraper.close()


def extract_app_metadata(apk_path, package_id, scraper=None):
    """Extracts app name from the APK and icon from APKMirror Scraper."""
    try:
        java_bin = load_config().get("settings", {}).get("java_path", "java")
        apkeditor_path = DATA_DIR / "bin" / "APKEditor.jar"

        app_name = package_id
        version_name = "Unknown"

        if apkeditor_path.exists():
            result = run_child(
                [java_bin, "-jar", str(apkeditor_path), "info", "-i", str(apk_path)],
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                lines = result.stdout.splitlines()
                info = {}
                for line in lines:
                    if "=" in line:
                        key, value = line.split("=", 1)
                        info[key.strip()] = value.strip().strip('"')
                app_name = info.get("AppName", package_id)
                version_name = info.get("VersionName", "Unknown")

        # Handle icon via scraper
        icon_dir = DATA_DIR / "apks" / "icons"
        icon_dir.mkdir(parents=True, exist_ok=True)
        target_icon = icon_dir / f"{package_id}.png"

        if not target_icon.exists():
            print(f"    [!] Icon missing for {package_id}, fetching from APKMirror...")
            close_scraper = False
            if scraper is None:
                scraper = ApkMirror_Downloader()
                close_scraper = True
            temporary_icon = icon_dir / f".{package_id}.{uuid.uuid4().hex}.png"

            try:
                app_data = scraper.search(package_name=package_id, only_release=True)
                if app_data and app_data.icon_url and app_data.icon_url != "Unknown":
                    downloaded_icon = scraper.download_file(
                        target_url=app_data.icon_url,
                        dest_directory=str(icon_dir),
                        filename=temporary_icon.name,
                    )
                    if downloaded_icon and temporary_icon.is_file():
                        os.replace(temporary_icon, target_icon)
            except Exception as e:
                print(f"    [!] Icon lookup failed for {package_id}: {e}")
            finally:
                try:
                    temporary_icon.unlink(missing_ok=True)
                except OSError as e:
                    print(
                        f"    [!] Could not remove temporary icon for {package_id}: {e}"
                    )
                if close_scraper:
                    try:
                        scraper.close()
                    except Exception as e:
                        if isinstance(
                            e,
                            (
                                AndroidVariantInspectionError,
                                AndroidVariantMetadataError,
                            ),
                        ):
                            raise
                        print(
                            f"    [!] Could not close icon scraper for {package_id}: {e}"
                        )

        # Save metadata
        metadata_file = DATA_DIR / "bin" / "metadata.json"
        metadata = {}
        if metadata_file.exists():
            with open(metadata_file, "r") as f:
                try:
                    metadata = json.load(f)
                except:
                    pass

        metadata[package_id] = {"real_name": app_name, "version": version_name}

        with open(metadata_file, "w") as f:
            json.dump(metadata, f, indent=4)

        return metadata[package_id]
    except Exception as e:
        print(f"    [!] Failed to extract metadata for {package_id}: {e}")
        return None


def get_app_preferences(package_name, config):
    """Scans patch sources to find if the app has a preferred apkFileType (APK vs BUNDLE)."""
    for source in config.get("sources", []):
        if not source.get("active", True):
            continue
        json_path = get_source_paths(source["repo"])[".json"]
        if not json_path.exists():
            continue
        try:
            with open(json_path, "r") as f:
                data = json.load(f)
                patches = data.get("patches", []) if isinstance(data, dict) else data
                for p in patches:
                    compat = p.get("compatiblePackages")
                    if not compat or not isinstance(compat, list):
                        continue
                    for pkg in compat:
                        if not isinstance(pkg, dict):
                            continue
                        p_id = pkg.get("packageName") or pkg.get("name")
                        if p_id == package_name:
                            ftype = pkg.get("apkFileType")
                            if ftype:
                                return ftype.upper()
        except:
            pass
    return None


def download_apk_via_apkeep(
    package_name: str, target_version: str, dest_directory: str
) -> Path | None:
    """Download an exact app version from APKPure with `apkeep`.

    Fallback for when APKMirror's Cloudflare challenge blocks the browser.

    Args:
        package_name: Android package id.
        target_version: Exact version to fetch.
        dest_directory: Existing directory the file is written into.

    Returns:
        The downloaded file, or None when apkeep is missing or the download failed.
    """
    apkeep = shutil.which("apkeep") or str(DATA_DIR / "bin" / "apkeep")
    if not os.path.exists(apkeep):
        print("    [!] apkeep is not installed; no fallback source available.")
        return None
    try:
        result = run_child(
            [
                apkeep,
                "-a",
                f"{package_name}@{target_version}",
                "-d",
                "apk-pure",
                dest_directory,
            ],
            capture_output=True,
            text=True,
            timeout=900,
        )
    except (subprocess.SubprocessError, OSError) as error:
        print(f"    [!] apkeep failed: {error}")
        return None
    if result.returncode != 0:
        print(
            f"    [!] apkeep exited {result.returncode}: {result.stderr.strip()[-300:]}"
        )
    downloads = sorted(
        path
        for path in Path(dest_directory).iterdir()
        if path.suffix in {".apk", ".xapk"}
    )
    return downloads[0] if downloads else None


def download_apk(
    package_name,
    target_version,
    final_path,
    tools_config,
    config,
    scraper=None,
    minimum_sdk: int | None = None,
):
    print(f"Downloading {package_name} v{target_version} using ApkMirror_Downloader...")
    java_bin = config.get("settings", {}).get("java_path", "java")

    close_scraper = False
    if scraper is None:
        scraper = ApkMirror_Downloader()
        close_scraper = True

    try:
        # Check for preferred file type from patches
        pref = get_app_preferences(package_name, config)

        prefer_bundle = False
        if pref:
            if "APK" in pref and "APKM" not in pref:
                print(
                    f"    [+] Source specifies standalone preference: {pref}. Prioritizing APK."
                )
                prefer_bundle = False
            elif any(x in pref for x in ["BUNDLE", "APKM", "XAPK"]):
                print(
                    f"    [+] Source specifies bundle preference: {pref}. Prioritizing Bundle."
                )
                prefer_bundle = True
        else:
            print(
                f"    [~] No patch preference found. Using default architecture/bundle settings..."
            )
            prefer_bundle = True  # Default to bundle as APKEditor can merge it cleanly

        app_data = scraper.search(
            package_name=package_name,
            version=target_version,
            only_release=True,
            prefer_bundle=prefer_bundle,
            minimum_sdk=minimum_sdk,
        )

        with tempfile.TemporaryDirectory(dir=Path(final_path).parent) as temp_dir:
            temp_file_name = f"{package_name}_{target_version}_download.tmp"

            source_name = "APKMirror"
            if app_data:
                downloaded_path = scraper.download_file(
                    target_url=app_data.download_url,
                    dest_directory=temp_dir,
                    filename=temp_file_name,
                )
            else:
                print(
                    f"    [~] APKMirror unavailable for {package_name} "
                    f"v{target_version}. Trying Uptodown..."
                )
                downloaded_path = scraper.download_from_uptodown(
                    package_name, target_version, temp_dir
                )
                if downloaded_path:
                    source_name = "Uptodown"
                    print(
                        f"    [+] Using Uptodown for {package_name} v{target_version}: "
                        "unsigned third-party mirror, checking the package id."
                    )
                else:
                    print(
                        f"    [~] Uptodown has no verified v{target_version}. "
                        "Trying APKPure via apkeep..."
                    )
                    source_name = "APKPure"
                    downloaded_path = download_apk_via_apkeep(
                        package_name, target_version, temp_dir
                    )
                    if not downloaded_path:
                        raise Exception(
                            f"Could not find {package_name} v{target_version} "
                            "on APKMirror, Uptodown or APKPure"
                        )

            if not downloaded_path or not os.path.exists(downloaded_path):
                raise Exception("Download failed or file not found.")

            # Download icon
            icon_dir = DATA_DIR / "apks" / "icons"
            icon_dir.mkdir(parents=True, exist_ok=True)
            if app_data and app_data.icon_url and app_data.icon_url != "Unknown":
                scraper.download_file(
                    target_url=app_data.icon_url,
                    dest_directory=str(icon_dir),
                    filename=f"{package_name}.png",
                )

            # Check if the downloaded file is a bundle by inspecting its zip contents
            import zipfile

            is_bundle_file = False
            try:
                with zipfile.ZipFile(downloaded_path, "r") as z:
                    if any(name.endswith(".apk") for name in z.namelist()):
                        is_bundle_file = True
            except:
                pass

            if is_bundle_file:
                print(f"Downloaded bundle. Merging splits via APKEditor...")
                apkeditor_path = DATA_DIR / tools_config["apkeditor_jar"]
                merge_command = [
                    java_bin,
                    "-jar",
                    str(apkeditor_path),
                    "m",
                    "-i",
                    downloaded_path,
                    "-o",
                    str(Path(temp_dir) / "merged.apk"),
                ]
                run_child(merge_command, check=True)
                candidate_path = Path(temp_dir) / "merged.apk"
                print(f"Successfully merged Universal APK to {final_path}")
            else:
                candidate_path = Path(downloaded_path)
                print(f"Successfully downloaded standalone APK to {final_path}")

            if source_name in ("Uptodown", "APKPure"):
                actual_package = _read_package_id(java_bin, candidate_path, "")
                if actual_package != package_name:
                    raise Exception(
                        f"{source_name} file does not verifiably contain package "
                        f"{package_name} (found {actual_package or 'unreadable'})"
                    )
                print(
                    f"    [+] Package id {package_name} confirmed in "
                    f"{source_name} file."
                )

            if minimum_sdk is not None:
                actual_minimum_sdk = _read_minimum_sdk(java_bin, candidate_path)
                if actual_minimum_sdk < minimum_sdk:
                    raise AndroidVariantUnavailableError(
                        "Downloaded APK does not meet the required Android version"
                    )
            os.replace(candidate_path, final_path)

            # Extract name from APK as a final check/metadata update
            extract_app_metadata(final_path, package_name, scraper=scraper)

    finally:
        if close_scraper:
            scraper.close()


# One line of morphe-cli output that reports an applied patch, as printed by
# v1.18.1: "Applied: <name>", optionally after a log level prefix.
APPLIED_LINE = re.compile(r"^\s*(?:(?:INFO|FINE)\s*:\s*)?Applied:\s+\S")

# How many trailing CLI output lines a zero-applied error quotes.
CLI_TAIL_LINES = 5


class PatchSelectionError(Exception):
    """Raised when a build cannot apply the patches the user selected.

    Covers a selected name that no bundle in the command carries and a CLI run
    that applied nothing. The CLI exits 0 in both cases, so the app checks.
    """


def _read_package_id(java_bin: str, input_apk: Path | str, fallback: str) -> str:
    """Read the real package id of an APK with APKEditor.

    Args:
        java_bin: Java binary to run.
        input_apk: Path of the APK to inspect.
        fallback: Id returned when the APK cannot be inspected.

    Returns:
        The package id from `APKEditor.jar info`, or `fallback`.
    """
    apkeditor_path = DATA_DIR / "bin" / "APKEditor.jar"
    try:
        info_result = run_child(
            [java_bin, "-jar", str(apkeditor_path), "info", "-i", str(input_apk)],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.error("Could not read package id of %s: %s", input_apk, e)
        return fallback
    for line in info_result.stdout.splitlines():
        if "package=" in line:
            return line.split("=", 1)[1].strip().strip('"')
    return fallback


def _read_minimum_sdk(java_bin: str, input_apk: Path | str) -> int:
    """Read an APK's manifest minSdk with the configured APKEditor CLI."""
    apkeditor_path = DATA_DIR / "bin" / "APKEditor.jar"
    try:
        result = run_child(
            [
                java_bin,
                "-jar",
                str(apkeditor_path),
                "info",
                "-i",
                str(input_apk),
                "-min-sdk-version",
                "-t",
                "json",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AndroidVariantInspectionError(
            "Could not verify the APK's Android version requirement"
        ) from error

    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        payload = None

    sdk_values: list[object] = []

    def collect_minimum_sdk(value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if (
                    str(key).replace("_", "").replace("-", "").casefold()
                    == "minsdkversion"
                ):
                    sdk_values.append(nested)
                else:
                    collect_minimum_sdk(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_minimum_sdk(nested)

    if payload is not None:
        collect_minimum_sdk(payload)
    if len(sdk_values) == 1 and type(sdk_values[0]) is int:
        return sdk_values[0]
    raise AndroidVariantMetadataError(
        "Could not verify the APK's Android version requirement"
    )


def _snapshot_apk(source: Path, scratch_dir: Path) -> Path:
    """Copy an atomically replaced cache APK into private build scratch space."""
    descriptor, snapshot_name = tempfile.mkstemp(
        prefix="validated-input-", suffix=".apk", dir=scratch_dir
    )
    os.close(descriptor)
    snapshot = Path(snapshot_name)
    try:
        shutil.copyfile(source, snapshot)
    except BaseException:
        snapshot.unlink(missing_ok=True)
        raise
    return snapshot


def compatible_patches(repo: str, package_id: str) -> list[dict[str, object]]:
    """Return one source's patch records that apply to a package.

    Args:
        repo: Source repo, `owner/repo`; its patch list must be on disk.
        package_id: Package the APK really has.

    Returns:
        Patch records compatible with the package, preserving source order.

    Raises:
        PatchListError: The patch list is missing, unreadable or malformed.
    """
    if not get_source_paths(repo)[".json"].exists():
        raise PatchListError(repo, "the file is missing")
    patches: list[dict[str, object]] = []
    for patch in load_patch_list(repo):
        comp = patch.get("compatiblePackages")
        if isinstance(comp, dict):
            is_compat = package_id in comp
        elif isinstance(comp, list):
            is_compat = any(
                isinstance(cp, dict)
                and package_id in (cp.get("packageName"), cp.get("name"))
                for cp in comp
            )
        else:
            is_compat = not comp
        name = patch.get("name")
        if is_compat and isinstance(name, str) and name:
            patches.append(patch)
    return patches


def _compatible_patch_names(repo: str, package_id: str) -> set[str]:
    """List casefolded names of patches from one repo for a package."""
    return {
        str(patch["name"]).casefold()
        for patch in compatible_patches(repo, package_id)
        if patch.get("name")
    }


def _compatible_patch_name_counts(repo: str, package_id: str) -> dict[str, int]:
    """Count casefolded compatible patch identities from one local source."""
    counts: dict[str, int] = {}
    for patch in compatible_patches(repo, package_id):
        folded = str(patch["name"]).casefold()
        counts[folded] = counts.get(folded, 0) + 1
    return counts


def _compatible_patch_name_index(repo: str, package_id: str) -> dict[str, str]:
    """Index compatible patch names by identity, retaining catalog spelling."""
    return {
        str(patch["name"]).casefold(): str(patch["name"])
        for patch in compatible_patches(repo, package_id)
    }


def resolve_patch_selection(
    app_config: dict,
    config: dict,
    *,
    require_all: bool = True,
    allow_unresolved: bool = False,
    warn_unavailable: bool = True,
    partial_preview: bool = False,
    preview_errors: list[tuple[str, list[str]]] | None = None,
) -> dict[str, list[str]]:
    """Resolve legacy names and validate source-keyed selections for one APK.

    Args:
        app_config: App configuration with legacy or source-keyed inclusions.
        config: Full configuration, including the ordered source list.
        require_all: Whether unresolved legacy names should raise an error.
        allow_unresolved: Return the applicable subset of mapped names instead
            of raising when a selected name is unavailable. Source-list and
            source-bundle errors still raise.
        warn_unavailable: Print warnings for selected mapped sources that are
            inactive or deleted.
        partial_preview: Skip unavailable mapped sources and stop legacy
            provider resolution when an earlier active source is unreadable.
        preview_errors: Optional list populated with unavailable source keys and
            affected selection names when `partial_preview` is enabled.

    Returns:
        Included patch names keyed by the configured repo spelling. Legacy
        names are assigned to the first active compatible source in config
        order; deleted and inactive mapped selections are retained in config
        but omitted here with a warning.

    Raises:
        PatchListError: A relevant active source list is missing or unreadable,
            or a mapped source has no bundle.
        PatchSelectionError: The selection shape, repo identity, or selected
            patch name is invalid or ambiguous, or a legacy name is unresolved
            when `require_all` is true and `allow_unresolved` is false.
    """
    package_id = app_config["id"]
    included = app_config.get("include_patches") or []
    sources_by_repo: dict[str, dict] = {}
    active_sources: list[dict] = []
    for source in config.get("sources", []):
        repo = source.get("repo")
        if not is_valid_repo(repo):
            continue
        key = repo.casefold()
        if key in sources_by_repo:
            raise PatchSelectionError("Duplicate source repositories in config")
        sources_by_repo[key] = source
        if source.get("active", True):
            active_sources.append(source)

    if isinstance(included, dict):
        requested: dict[str, list[str]] = {}
        for repo, names in included.items():
            if not is_valid_repo(repo) or not isinstance(names, list):
                raise PatchSelectionError("Invalid source-keyed patch selection")
            key = repo.casefold()
            if key in requested:
                raise PatchSelectionError("Duplicate source keys in patch selection")
            if not all(isinstance(name, str) and name for name in names):
                raise PatchSelectionError(
                    "Patch selection names must be non-empty text"
                )
            requested[key] = list(dict.fromkeys(names))

        resolved: dict[str, list[str]] = {}
        resolved_keys: set[str] = set()
        for source in active_sources:
            repo = source["repo"]
            repo_key = repo.casefold()
            names = requested.get(repo_key)
            if not names:
                continue
            resolved_keys.add(repo_key)
            try:
                _reject_source_path_alias(repo, config.get("sources", []))
            except PatchSelectionError:
                if not partial_preview:
                    raise
                if preview_errors is not None:
                    preview_errors.append((repo_key, names))
                continue
            paths = get_source_paths(repo)
            if not paths[".mpp"].is_file():
                if partial_preview:
                    if preview_errors is not None:
                        preview_errors.append((repo_key, names))
                    continue
                raise PatchListError(repo, "the selected patch bundle is missing")
            try:
                name_counts = _compatible_patch_name_counts(repo, package_id)
            except PatchListError:
                if not partial_preview:
                    raise
                if preview_errors is not None:
                    preview_errors.append((repo_key, names))
                continue
            carries = set(name_counts)
            missing = [name for name in names if name.casefold() not in carries]
            if missing and not allow_unresolved:
                raise PatchSelectionError("Selected patch is unavailable")
            ambiguous_names = [
                name for name in names if name_counts.get(name.casefold(), 0) > 1
            ]
            if ambiguous_names and not partial_preview:
                raise PatchSelectionError(
                    "Selected patch name is ambiguous in its source"
                )
            if ambiguous_names and preview_errors is not None:
                preview_errors.append((repo_key, ambiguous_names))
            canonical_names = _compatible_patch_name_index(repo, package_id)
            matched = [
                name
                for name in names
                if name.casefold() in carries
                and name_counts.get(name.casefold(), 0) == 1
            ]
            if matched:
                resolved[repo] = [
                    canonical_names[name.casefold()] if partial_preview else name
                    for name in matched
                ]
        for repo_key in requested.keys() - resolved_keys:
            if warn_unavailable:
                print("    [!] Selected patch source is missing or inactive; skipping")
        if any(requested.values()) and not resolved and not allow_unresolved:
            raise PatchSelectionError("none of the selected patch sources are active")
        return resolved

    if not isinstance(included, list) or not all(
        isinstance(name, str) and name for name in included
    ):
        raise PatchSelectionError("Patch selection must be a list or source mapping")

    include_order = list(dict.fromkeys(included))
    remaining = {name.casefold() for name in include_order}
    resolved = {}
    legacy_preview_blocked = False
    for source in active_sources:
        if not remaining:
            break
        repo = source["repo"]
        if not get_source_paths(repo)[".json"].exists():
            if partial_preview and remaining:
                legacy_preview_blocked = True
                if preview_errors is not None:
                    preview_errors.append(
                        (
                            repo.casefold(),
                            [
                                name
                                for name in include_order
                                if name.casefold() in remaining
                            ],
                        )
                    )
                break
            continue
        try:
            name_counts = _compatible_patch_name_counts(repo, package_id)
        except PatchListError:
            if not partial_preview:
                raise
            legacy_preview_blocked = True
            if preview_errors is not None:
                preview_errors.append(
                    (
                        repo.casefold(),
                        [
                            name
                            for name in include_order
                            if name.casefold() in remaining
                        ],
                    )
                )
            break
        carries = set(name_counts)
        found = remaining & carries
        if not found:
            continue
        ambiguous_folded = {name for name in found if name_counts[name] > 1}
        if ambiguous_folded and not partial_preview:
            raise PatchSelectionError("Selected patch name is ambiguous in its source")
        if ambiguous_folded:
            legacy_preview_blocked = True
            if preview_errors is not None:
                preview_errors.append(
                    (
                        repo.casefold(),
                        [
                            name
                            for name in include_order
                            if name.casefold() in ambiguous_folded
                        ],
                    )
                )
            remaining -= ambiguous_folded
            found -= ambiguous_folded
        if not found:
            continue
        if partial_preview and not get_source_paths(repo)[".mpp"].is_file():
            legacy_preview_blocked = True
            if preview_errors is not None:
                preview_errors.append(
                    (
                        repo.casefold(),
                        [name for name in include_order if name.casefold() in found],
                    )
                )
            break
        try:
            _reject_source_path_alias(repo, config.get("sources", []))
        except PatchSelectionError:
            if not partial_preview:
                raise
            legacy_preview_blocked = True
            if preview_errors is not None:
                preview_errors.append(
                    (
                        repo.casefold(),
                        [name for name in include_order if name.casefold() in found],
                    )
                )
            break
        canonical_names = _compatible_patch_name_index(repo, package_id)
        resolved[repo] = [
            canonical_names[name.casefold()] if partial_preview else name
            for name in include_order
            if name.casefold() in found
        ]
        remaining -= found

    base_key = BASE_PATCHES_REPO.casefold()
    if remaining and base_key not in sources_by_repo and not legacy_preview_blocked:
        base_bundle = get_source_paths(BASE_PATCHES_REPO)[".mpp"]
        if base_bundle.is_file():
            name_counts = _compatible_patch_name_counts(BASE_PATCHES_REPO, package_id)
            carries = set(name_counts)
            found = remaining & carries
            if found:
                ambiguous_folded = {name for name in found if name_counts[name] > 1}
                if ambiguous_folded and not partial_preview:
                    raise PatchSelectionError(
                        "Selected patch name is ambiguous in its source"
                    )
                if ambiguous_folded:
                    if preview_errors is not None:
                        preview_errors.append(
                            (
                                base_key,
                                [
                                    name
                                    for name in include_order
                                    if name.casefold() in ambiguous_folded
                                ],
                            )
                        )
                    remaining -= ambiguous_folded
                    found -= ambiguous_folded
                if not found:
                    return dict(
                        sorted(resolved.items(), key=lambda item: item[0].casefold())
                    )
                _reject_source_path_alias(BASE_PATCHES_REPO, config.get("sources", []))
                if not base_bundle.is_file():
                    raise PatchListError(
                        BASE_PATCHES_REPO, "the selected patch bundle is missing"
                    )
                canonical_names = _compatible_patch_name_index(
                    BASE_PATCHES_REPO, package_id
                )
                resolved[BASE_PATCHES_REPO] = [
                    canonical_names[name.casefold()] if partial_preview else name
                    for name in include_order
                    if name.casefold() in found
                ]
                remaining -= found

    if remaining and require_all:
        raise PatchSelectionError(
            "No active patch source provides every selected patch"
        )
    return dict(sorted(resolved.items(), key=lambda item: item[0].casefold()))


def required_minimum_sdk(app_config: dict, config: dict) -> int | None:
    """Return the artifact SDK floor for an app's resolved selected providers.

    The HushFacebook requirement is limited to Facebook and applies only when
    that repository actually provides a selected patch. Legacy flat selections
    use the same config-order resolution as builds.

    Args:
        app_config: App entry with package id and patch selection.
        config: Full configuration with ordered sources.

    Returns:
        The required Android API level, or None when no constraint applies.

    Raises:
        PatchListError: A relevant source list or bundle is unavailable.
    """
    package_id = app_config.get("id")
    if not isinstance(package_id, str) or package_id.casefold() != HUSHFACEBOOK_PACKAGE:
        return None

    try:
        selected_repos = resolve_patch_selection(app_config, config)
    except PatchSelectionError:
        return None
    if any(
        repo.casefold() == HUSHFACEBOOK_REPO.casefold() and names
        for repo, names in selected_repos.items()
    ):
        return HUSHFACEBOOK_MINIMUM_SDK
    return None


def patch_app(
    app_config: dict,
    tools_config: dict,
    input_apk: Path | str,
    output_apk: Path | str,
    config: dict,
) -> tuple[bool, list[str]]:
    """Run morphe-cli on an APK with each selector grouped under its bundle.

    Names are matched to bundles ignoring case; the command carries the
    spelling from the saved selection. Before the CLI runs, every selected
    name must be carried by a bundle that fits the APK. The CLI exits 0 even
    when it applies nothing, so its output is read afterwards: with a
    selection, at least one line of the form "Applied: <name>" (optionally
    after an INFO or FINE level prefix) is required.

    Args:
        app_config: App entry with id, name, include_patches, exclude_patches.
        tools_config: Tool paths; must hold cli_jar.
        input_apk: Path of the APK to patch.
        output_apk: Path of the patched APK to write.
        config: Full config with sources and settings.

    Returns:
        Tuple of success flag and the repos of the bundles that were used.

    Raises:
        PatchSelectionError: If a selected name is unknown to every bundle,
            there are no active selected sources, or the CLI applies no selected
            patches. The output APK is removed when the CLI applies none.
        PatchListError: If the patch list of a source that is needed, or of the
            base bundle used as fallback, is missing or unreadable.
    """
    cli = DATA_DIR / tools_config["cli_jar"]
    java_bin = config.get("settings", {}).get("java_path", "java")
    app_name = app_config["name"]
    input_package_id = _read_package_id(java_bin, input_apk, app_config["id"])

    raw_selection = app_config.get("include_patches") or []
    resolved_selection = resolve_patch_selection(app_config, config)
    has_selected_patches = (
        any(raw_selection.values())
        if isinstance(raw_selection, dict)
        else bool(raw_selection)
    )
    if has_selected_patches and not resolved_selection:
        raise PatchSelectionError("none of the selected patch sources are active")

    included_mpps: dict[str, str] = {}  # path -> repo
    bundle_includes: dict[str, list[str]] = {}  # path -> names enabled via -e
    bundle_carries: dict[str, set[str]] = {}  # path -> casefolded names it carries
    for repo, names in resolved_selection.items():
        paths = get_source_paths(repo)
        key = str(paths[".mpp"])
        included_mpps[key] = repo
        bundle_includes[key] = names
        bundle_carries[key] = _compatible_patch_names(repo, input_package_id)

    if not included_mpps:
        default_mpp = get_source_paths(BASE_PATCHES_REPO)[".mpp"]
        if default_mpp.is_file():
            included_mpps[str(default_mpp)] = BASE_PATCHES_REPO
            bundle_includes[str(default_mpp)] = []
            bundle_carries[str(default_mpp)] = set()

    command = [
        java_bin,
        "-jar",
        str(cli),
        "patch",
        "--continue-on-error",
        "--exclusive",
        str(input_apk),
        "-o",
        str(output_apk),
    ]

    # morphe-cli binds -e/-d to the --patches bundle they follow, so each
    # bundle is emitted with only the selectors for names that live in it.
    bundle_excludes: dict[str, list[str]] = {path: [] for path in included_mpps}
    for name in app_config.get("exclude_patches") or []:
        folded = name.casefold()
        for path in included_mpps:
            if folded in bundle_carries[path]:
                bundle_excludes[path].append(name)
                break

    for mpp_path in included_mpps:
        command.extend(["--patches", mpp_path])
        for name in bundle_includes.get(mpp_path, []):
            command.extend(["-e", name])
        for name in bundle_excludes[mpp_path]:
            command.extend(["-d", name])

    print(f"Running patcher for {app_name}...")
    has_severe_error = False
    applied_count = 0
    recent_lines: deque[str] = deque(maxlen=CLI_TAIL_LINES)
    output_path = Path(output_apk)
    try:
        process = start_child(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        assert process.stdout is not None
        try:
            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                if not line:
                    continue
                if "SEVERE:" in line:
                    has_severe_error = True
                if APPLIED_LINE.match(line):
                    applied_count += 1
                if line.strip():
                    recent_lines.append(line.strip())
                sys.stdout.write(secrets_store.redact(line))
                sys.stdout.flush()
        except BaseException:
            # A failed read must not leave the child running or a partial APK.
            process.kill()
            process.wait()
            output_path.unlink(missing_ok=True)
            raise

        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, command)

        if has_severe_error:
            print(
                f"\n[!] {app_name} patched with some SEVERE errors. "
                "Deleting failed APK."
            )
            output_path.unlink(missing_ok=True)
            return False, []

        selected_names = [
            name for names in resolved_selection.values() for name in names
        ]
        if selected_names and applied_count == 0:
            output_path.unlink(missing_ok=True)
            raise PatchSelectionError("Selected patches were not applied")

        print(f"Successfully patched {app_name} to {output_apk}")
        return True, list(included_mpps.values())
    except PatchSelectionError:
        raise
    except (OSError, subprocess.SubprocessError) as e:
        print(f"Patching failed for {app_name}: {e}")
        output_path.unlink(missing_ok=True)
        return False, []


def save_metadata(package_id, data):
    """Saves app-specific metadata to the persistent store."""
    metadata_file = DATA_DIR / "bin" / "metadata.json"
    metadata = {}
    if metadata_file.exists():
        with open(metadata_file, "r") as f:
            try:
                metadata = json.load(f)
            except:
                pass

    if package_id not in metadata:
        metadata[package_id] = {}

    metadata[package_id].update(data)

    with open(metadata_file, "w") as f:
        json.dump(metadata, f, indent=4)
    return metadata[package_id]


def get_patch_versions(config):
    """Returns a dictionary of current versions for all active patch sources."""
    local_versions = load_local_versions()
    patch_info = {}
    for source in config.get("sources", []):
        if source.get("active", True):
            patch_info[source["repo"]] = local_versions.get(source["repo"], "Unknown")
    return patch_info


def get_current_version(package_id):
    """Finds the latest version of the raw APK we have locally."""
    raw_dir = DATA_DIR / "apks" / "raw"
    if not raw_dir.exists():
        return None

    versions_found = []
    for f in raw_dir.glob(f"{package_id}-*.apk"):
        # Extract version from filename: pkg.id-1.2.3.apk
        v_str = f.name.replace(f"{package_id}-", "").replace(".apk", "")
        try:
            versions_found.append(version.parse(v_str))
        except:
            continue

    if not versions_found:
        return None
    return str(max(versions_found))


def run_pipeline(package_ids=None, quiet=False, scraper=None):
    config = load_config()
    try:
        was_updated = update_tools(config, quiet=quiet)
    except (GitHubRateLimitError, GitHubTokenRejectedError) as e:
        print(f"[x] {e} Continuing with the tools and patches already on disk.")
        was_updated = False

    # Get current versions of all active patch sources
    current_patch_versions = get_patch_versions(config)

    raw_dir = DATA_DIR / "apks" / "raw"
    patched_dir = DATA_DIR / "apks" / "patched"
    raw_dir.mkdir(parents=True, exist_ok=True)
    patched_dir.mkdir(parents=True, exist_ok=True)
    # Same filesystem as patched_dir so the final move is an atomic rename.
    # Anything left here was abandoned by a killed build (builds are serialised).
    scratch_dir = DATA_DIR / "apks" / ".building"
    shutil.rmtree(scratch_dir, ignore_errors=True)
    scratch_dir.mkdir(parents=True)

    apps_to_build = config["apps"]
    if package_ids:
        apps_to_build = [a for a in apps_to_build if a["id"] in package_ids]

    buildable_apps = []
    for app in apps_to_build:
        selection = app.get("include_patches") or []
        has_selected_patches = isinstance(selection, dict) and any(selection.values())
        if has_selected_patches:
            try:
                resolved = resolve_patch_selection(app, config)
                if not resolved:
                    raise PatchSelectionError(
                        "none of the selected patch sources are active"
                    )
            except (PatchListError, PatchSelectionError) as error:
                print(f"[x] {error}")
                save_app_result(app["id"], status="Error", error=str(error))
                continue
        buildable_apps.append(app)
    apps_to_build = buildable_apps
    if not apps_to_build:
        return []

    patched_apps = []

    close_scraper = False
    private_snapshots: list[Path] = []
    if scraper is None:
        scraper = ApkMirror_Downloader()
        close_scraper = True

    try:
        for i, app in enumerate(apps_to_build):
            try:
                # Mimic human behavior with small delays between apps
                if i > 0:
                    time.sleep(5)

                # Check for patch source updates handled in main loop

                supported, _, _ = get_supported_versions(
                    app["id"], config, scraper=scraper, force_refresh=True
                )

                # PINNED VERSION LOGIC:
                # ...
                pinned_version = app.get("version")
                if pinned_version:
                    print(
                        f"[!] Pinned version {pinned_version} detected for {app['id']}. Ignoring other versions."
                    )
                    supported = [pinned_version]

                minimum_sdk = required_minimum_sdk(app, config)

                if not supported:
                    print(f"[!] No supported versions found for {app['id']}")
                    save_app_result(
                        app["id"], status="Error", error="No supported versions"
                    )
                    continue

                download_success = False
                target_version = None
                raw_apk_path = None
                patch_input_path = None

                for v in supported:
                    raw_apk_path = raw_dir / f"{app['id']}-{v}.apk"
                    if raw_apk_path.exists():
                        if minimum_sdk is None:
                            target_version = v
                            download_success = True
                            print(
                                f"Raw APK already exists for {app['id']} v{v}, skipping download."
                            )
                            extract_app_metadata(
                                raw_apk_path, app["id"], scraper=scraper
                            )
                            break
                        cached_snapshot = _snapshot_apk(raw_apk_path, scratch_dir)
                        private_snapshots.append(cached_snapshot)
                        cached_minimum_sdk = _read_minimum_sdk(
                            config.get("settings", {}).get("java_path", "java"),
                            cached_snapshot,
                        )
                        if cached_minimum_sdk >= minimum_sdk:
                            target_version = v
                            download_success = True
                            patch_input_path = cached_snapshot
                            break
                        cached_snapshot.unlink(missing_ok=True)
                        private_snapshots.remove(cached_snapshot)
                        print(
                            "Cached APK fails Android version validation; replacing it."
                        )

                    try:
                        download_apk(
                            app["id"],
                            v,
                            raw_apk_path,
                            config["tools"],
                            config,
                            scraper=scraper,
                            minimum_sdk=minimum_sdk,
                        )
                        if minimum_sdk is not None:
                            download_snapshot = _snapshot_apk(raw_apk_path, scratch_dir)
                            private_snapshots.append(download_snapshot)
                            if (
                                _read_minimum_sdk(
                                    config.get("settings", {}).get("java_path", "java"),
                                    download_snapshot,
                                )
                                < minimum_sdk
                            ):
                                raise AndroidVariantUnavailableError(
                                    "Downloaded APK does not meet the required Android version"
                                )
                            patch_input_path = download_snapshot
                        target_version = v
                        download_success = True
                        break
                    except Exception as e:
                        print(
                            f"    [!] Failed to download v{v}: {e}. Trying next version..."
                        )
                        if (
                            minimum_sdk is None
                            and raw_apk_path
                            and raw_apk_path.exists()
                        ):
                            raw_apk_path.unlink(
                                missing_ok=True
                            )  # Cleanup partial download
                        continue

                if not download_success:
                    # ...
                    print(
                        f"[x] Could not download any supported version for {app['id']}"
                    )
                    save_app_result(app["id"], status="Error", error="Download failed")
                    continue

                # ... patching logic ...
                last_build = app.get("last_successful_build", {})

                if not was_updated and is_build_current(
                    last_build,
                    target_version,
                    app,
                    current_patch_versions,
                    patched_dir,
                    config,
                ):
                    recorded = last_build.get("combined_version")
                    print(
                        f"[\u2713] {app['name']} is already up to date "
                        f"(Version: {recorded}), skipping patch."
                    )
                    continue

                # Patch into scratch space so the served APK stays untouched
                # until the new one is complete.
                with tempfile.TemporaryDirectory(dir=scratch_dir) as scratch:
                    scratch_apk = Path(scratch) / f"{app['id']}.apk"
                    try:
                        success, used_repos = patch_app(
                            app,
                            config["tools"],
                            patch_input_path or raw_apk_path,
                            scratch_apk,
                            config,
                        )
                    except PatchSelectionError as e:
                        print(f"[x] {e}")
                        save_app_result(app["id"], status="Failed", error=str(e))
                        continue
                    if success and not scratch_apk.is_file():
                        print(f"[!] Patcher wrote no APK for {app['id']}")
                        success = False
                    if success:
                        # Only save versions for the repos actually used for this app
                        relevant_patch_versions = {
                            repo: current_patch_versions.get(repo, "Unknown")
                            for repo in used_repos
                        }
                        combined_version = build_combined_version(
                            target_version,
                            relevant_patch_versions,
                            effective_build_selection(app, config),
                        )
                        patched_apk_path = (
                            patched_dir / f"{app['id']}-{combined_version}.apk"
                        )
                        os.replace(scratch_apk, patched_apk_path)

                if success:
                    patched_apps.append(app["id"])

                    build_record = {
                        "app_version": target_version,
                        "patch_versions": relevant_patch_versions,
                        "combined_version": combined_version,
                        "filename": patched_apk_path.name,
                        "patches": effective_build_selection(app, config),
                        "build_layout": BUILD_LAYOUT,
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    save_app_result(
                        app["id"],
                        status="Success",
                        error=None,
                        last_successful_build=build_record,
                    )
                    # The new APK is in place and recorded; drop the older ones.
                    for old_file in patched_dir.glob(f"{app['id']}-*.apk"):
                        if old_file == patched_apk_path:
                            continue
                        try:
                            old_file.unlink(missing_ok=True)
                        except OSError as e:
                            print(f"    [!] Could not remove {old_file.name}: {e}")
                else:
                    save_app_result(
                        app["id"],
                        status="Failed",
                        error="Patching failed with SEVERE errors",
                    )
            except Exception as e:
                print(f"Error processing {app['id']}: {e}")
                save_app_result(app["id"], status="Error", error=str(e))
    finally:
        for snapshot in private_snapshots:
            snapshot.unlink(missing_ok=True)
        if close_scraper:
            scraper.close()

    return patched_apps


def get_all_available_apps(config):
    """Scans all active source JSONs and returns a unique list of supported apps with translated names."""
    apps_data = {}  # pkg_id -> name
    for source in config.get("sources", []):
        if not source.get("active", True):
            continue

        json_path = get_source_paths(source["repo"])[".json"]
        if not json_path.exists():
            continue

        with open(json_path, "r") as f:
            try:
                data = json.load(f)
            except:
                continue

        app_names_map = data.get("appNames") if isinstance(data, dict) else None
        if not isinstance(app_names_map, dict):
            app_names_map = {}
        patches_list = data.get("patches", []) if isinstance(data, dict) else data
        if not isinstance(patches_list, list):
            continue
        for patch in patches_list:
            if not isinstance(patch, dict):
                continue
            compat_pkgs = patch.get("compatiblePackages")
            if not compat_pkgs:
                continue

            pkgs_to_add = []
            entry_names = {}
            if isinstance(compat_pkgs, dict):
                pkgs_to_add = list(compat_pkgs.keys())
            elif isinstance(compat_pkgs, list):
                pkgs_to_add = []
                for pkg in compat_pkgs:
                    if not isinstance(pkg, dict):
                        continue
                    # Prioritize packageName (ReVanced style), then name
                    p_id = pkg.get("packageName") or pkg.get("name")
                    if p_id:
                        pkgs_to_add.append(p_id)
                        # With a packageName present, 'name' is the display name
                        if pkg.get("packageName") and pkg.get("name"):
                            entry_names[str(p_id)] = pkg["name"]

            for pkg_id in pkgs_to_add:
                if not pkg_id:
                    continue
                pkg_id = str(pkg_id)
                translated_name = entry_names.get(pkg_id) or app_names_map.get(pkg_id)
                if translated_name is not None and not isinstance(translated_name, str):
                    translated_name = None  # a name that is not text is ignored
                # If we don't have a name yet, or we found a translated one, set it
                if pkg_id not in apps_data or translated_name:
                    apps_data[pkg_id] = translated_name or pkg_id

    # Return list of objects sorted by name
    result = [{"pkg": pkg, "name": name} for pkg, name in apps_data.items()]
    return sorted(result, key=lambda x: x["name"].lower())


if __name__ == "__main__":
    secrets_store.absorb_environment()
    run_pipeline()
