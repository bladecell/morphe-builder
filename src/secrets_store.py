"""Two secrets kept in DATA_DIR/.env, never in os.environ or the config.

Only `GITHUB_TOKEN` and `CLOAKBROWSER_LICENSE_KEY` are recognised. A real
environment variable wins over the file. Other lines and comments in the file are
kept verbatim when it is rewritten but never loaded. Values are validated so a
value cannot carry a newline (and so another variable) into the file, and they
are never logged: callers log only names.

`config_store.DATA_DIR` is read at call time so tests can redirect it. Writes
are atomic, mode 0600, and serialised by one process-wide lock.

WARNING: DATA_DIR/.env must never be loaded as a process environment file
(compose `env_file`, `uvicorn --env-file`, a shell `source`). A value set from
the network would then count as environment-sourced and be locked, and the
file is not shell syntax: values are limited to `[A-Za-z0-9._+/=:@-]`
so it stays harmless if it is loaded by mistake.
"""

import logging
import os
import re
import threading
from pathlib import Path
from typing import Literal

from src import config_store

logger = logging.getLogger(__name__)

GITHUB_TOKEN = "GITHUB_TOKEN"
CLOAKBROWSER_LICENSE_KEY = "CLOAKBROWSER_LICENSE_KEY"
SECRET_NAMES = (GITHUB_TOKEN, CLOAKBROWSER_LICENSE_KEY)

Source = Literal["environment", "file"]

# Tokens and licence keys only: no shell syntax, quotes, whitespace or control
# characters (splitlines also splits on \x0b, \x0c, \x1c-\x1e, \x85, U+2028).
# fullmatch rejects a trailing newline too.
_VALUE_PATTERN = re.compile(r"[A-Za-z0-9._+/=:@-]{8,512}")
_LINE_PATTERN = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)")
_FILE_LOCK = threading.Lock()


class InvalidSecretError(ValueError):
    """The value is not an acceptable secret. The message never holds the value."""


class SecretFromEnvironmentError(RuntimeError):
    """The secret comes from the environment and cannot be changed here."""


def _env_path() -> Path:
    """Return the .env path under the current data directory."""
    return config_store.DATA_DIR / ".env"


def _env_value(name: str) -> str | None:
    """Return the stripped environment value of `name`, None when blank or unset."""
    return (os.environ.get(name) or "").strip() or None


def _check_name(name: str) -> None:
    """Raise KeyError unless `name` is one of the two recognised secrets."""
    if name not in SECRET_NAMES:
        raise KeyError(name)


def _read_lines() -> list[str]:
    """Return the lines of the .env file, or an empty list when it is missing.

    Raises:
        OSError: The file exists but could not be read.
    """
    try:
        return _env_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []


def _line_name(line: str) -> str | None:
    """Return the recognised secret name a line assigns, else None."""
    match = _LINE_PATTERN.fullmatch(line)
    if match is None or match.group(1) not in SECRET_NAMES:
        return None
    return match.group(1)


def _file_value(name: str) -> str | None:
    """Return the usable value of `name` in the file, or None.

    A malformed or invalid entry counts as unset and is reported by name only.
    The last assignment wins, as in a shell.

    Raises:
        OSError: The file exists but could not be read.
    """
    value: str | None = None
    for line in _read_lines():
        match = _LINE_PATTERN.fullmatch(line)
        if match is None or match.group(1) != name:
            continue
        value = match.group(2).strip()
    if value is None:
        return None
    if _VALUE_PATTERN.fullmatch(value) is None:
        logger.warning("Ignoring invalid value for %s in the .env file", name)
        return None
    return value


def get_secret(name: str) -> str | None:
    """Return the current value of a secret, the environment first.

    Args:
        name: `GITHUB_TOKEN` or `CLOAKBROWSER_LICENSE_KEY`.

    Returns:
        The value, or None when it is set nowhere.

    Raises:
        KeyError: `name` is not a recognised secret.
        OSError: The file exists but could not be read.
    """
    _check_name(name)
    return _env_value(name) or _file_value(name)


def secret_source(name: str) -> Source | None:
    """Report where a secret's value comes from.

    Args:
        name: `GITHUB_TOKEN` or `CLOAKBROWSER_LICENSE_KEY`.

    Returns:
        "environment", "file", or None when unset.

    Raises:
        KeyError: `name` is not a recognised secret.
        OSError: The file exists but could not be read.
    """
    _check_name(name)
    if _env_value(name):
        return "environment"
    return "file" if _file_value(name) is not None else None


def get_status() -> dict[str, Source | None]:
    """Report the source of every recognised secret, without any value.

    Returns:
        Source ("environment", "file" or None) keyed by secret name.

    Raises:
        OSError: The file exists but could not be read.
    """
    return {name: secret_source(name) for name in SECRET_NAMES}


def validate_secret(value: object) -> str:
    """Check that `value` is safe to store.

    Args:
        value: Candidate secret.

    Returns:
        The value, unchanged.

    Raises:
        InvalidSecretError: Not a string of 8 to 512 printable ASCII characters
            without whitespace, quotes or backticks.
    """
    if not isinstance(value, str) or _VALUE_PATTERN.fullmatch(value) is None:
        raise InvalidSecretError("Invalid secret value")
    return value


def _rewrite(name: str, value: str | None) -> None:
    """Set or remove `name` in the file, keeping every other line as it was.

    Raises:
        SecretFromEnvironmentError: The environment already provides `name`.
        OSError: The file could not be read or written.
    """
    with _FILE_LOCK:
        if _env_value(name):
            raise SecretFromEnvironmentError(name)
        # An interrupted write can leave a temp copy holding both secrets.
        for stale in _env_path().parent.glob("..env-*"):
            stale.unlink(missing_ok=True)
        kept = [line for line in _read_lines() if _line_name(line) != name]
        if value is not None:
            kept.append(f"{name}={value}")
        config_store.write_file_atomic(
            _env_path(), "".join(f"{line}\n" for line in kept), mode=0o600
        )


def set_secret(name: str, value: str) -> None:
    """Store a secret in the .env file, replacing any earlier assignment.

    Args:
        name: `GITHUB_TOKEN` or `CLOAKBROWSER_LICENSE_KEY`.
        value: The secret; see `validate_secret`.

    Raises:
        KeyError: `name` is not a recognised secret.
        InvalidSecretError: `value` is not acceptable.
        SecretFromEnvironmentError: The environment already provides `name`.
        OSError: The file could not be read or written.
    """
    _check_name(name)
    _rewrite(name, validate_secret(value))


def clear_secret(name: str) -> None:
    """Remove a secret from the .env file.

    Args:
        name: `GITHUB_TOKEN` or `CLOAKBROWSER_LICENSE_KEY`.

    Raises:
        KeyError: `name` is not a recognised secret.
        SecretFromEnvironmentError: The environment provides `name`.
        OSError: The file could not be read or written.
    """
    _check_name(name)
    _rewrite(name, None)


def redact(text: str) -> str:
    """Replace every current secret value in `text` with "***".

    Args:
        text: Any string, such as an error message about to be logged.

    Returns:
        `text` with the values removed. Unreadable files are treated as empty.
    """
    for name in SECRET_NAMES:
        try:
            value = get_secret(name)
        except OSError:
            value = _env_value(name)
        if value:
            text = text.replace(value, "***")
    return text
