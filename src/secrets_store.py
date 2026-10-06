"""Two secrets kept in DATA_DIR/.env or the environment, never in the config.

Only `GITHUB_TOKEN` and `CLOAKBROWSER_LICENSE_KEY` are recognised. A real
environment variable wins over the file, and is held to the same value pattern:
an invalid one is ignored (never returned, no fallback to the file), reported as
"environment_invalid" in the status and named once in the log. Other lines and
comments in the file are kept verbatim when it is rewritten but never loaded.
Values are validated so a value cannot carry a newline (and so another variable)
into the file, and they are never logged: callers log only names.

At startup `absorb_environment` moves both variables out of `os.environ` into
memory, so nothing the app starts afterwards (a subprocess, the browser, library
code) inherits them. The store keeps reporting them as coming from the
environment and keeps validating and locking them. Nothing here ever writes a
secret into `os.environ`.

Every value that has really been in effect in this process (read from the
environment or the file, or successfully written) stays masked by `redact`,
even after it was rotated or cleared. A value that was refused or whose write
failed is never remembered, and the values replaced since are kept in a bounded
list (`MAX_RETIRED_VALUES`, oldest dropped first); the values currently in
effect are never dropped.

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
import traceback
from pathlib import Path
from typing import Literal

from src import config_store

logger = logging.getLogger(__name__)

GITHUB_TOKEN = "GITHUB_TOKEN"
CLOAKBROWSER_LICENSE_KEY = "CLOAKBROWSER_LICENSE_KEY"
SECRET_NAMES = (GITHUB_TOKEN, CLOAKBROWSER_LICENSE_KEY)

Source = Literal["environment", "environment_invalid", "file"]

# Tokens and licence keys only: no shell syntax, quotes, whitespace or control
# characters (splitlines also splits on \x0b, \x0c, \x1c-\x1e, \x85, U+2028).
# fullmatch rejects a trailing newline too.
_VALUE_PATTERN = re.compile(r"[A-Za-z0-9._+/=:@-]{8,512}")
_LINE_PATTERN = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)")
_FILE_LOCK = threading.Lock()
_warned_invalid_env: set[str] = set()
_warned_invalid_file: set[str] = set()
# Values taken out of os.environ by `absorb_environment`, by variable name.
_captured_environment: dict[str, str] = {}
# Shorter values are not remembered: masking them would corrupt ordinary text.
_MIN_MASKED_LENGTH = 8
# Values in effect now, by (secret name, "environment" or "file"). At most four.
_current: dict[tuple[str, str], str] = {}
# Values that were in effect and have been replaced or cleared, oldest first.
# Bounded: a flood of real rotations can only push out older retired values.
MAX_RETIRED_VALUES = 32
_retired: tuple[str, ...] = ()
# The two groups above, longest first. Replaced as a whole (never mutated), so
# `redact` can read it without a lock.
_known: tuple[str, ...] = ()
# The most that `_known` can hold: the retired values plus one per name and source.
MAX_REMEMBERED_VALUES = MAX_RETIRED_VALUES + 2 * len(SECRET_NAMES)
# A value being written right now. Masked from before the write until it is
# either in effect (then it is in `_current`) or dropped, so a log line made
# during the write cannot show it.
_pending: tuple[str, ...] = ()
_known_lock = threading.Lock()


class InvalidSecretError(ValueError):
    """The value is not an acceptable secret. The message never holds the value."""


class SecretFromEnvironmentError(RuntimeError):
    """The secret comes from the environment and cannot be changed here.

    Attributes:
        invalid: True when the variable is set but its value is not a valid
            secret (so it is ignored), False when it is in use.
    """

    def __init__(self, name: str, invalid: bool = False) -> None:
        super().__init__(name)
        self.invalid = invalid


def _track(name: str, source: str, value: str | None) -> None:
    """Record the value now in effect for a secret and source; retire the old one.

    A value that is replaced or removed moves to the bounded retired list, so it
    stays masked. Only values in effect get here: the environment's, the file's
    and a successfully written one.

    Args:
        name: Secret name.
        source: "environment" or "file".
        value: The value in effect, or None when there is none any more.
    """
    global _retired, _known
    key = (name, source)
    if _current.get(key) == value:
        return
    with _known_lock:
        old = _current.get(key)
        if old == value:
            return
        if value is None:
            _current.pop(key, None)
        else:
            _current[key] = value
        if old is not None and old not in _current.values() and old not in _retired:
            _retired = (*_retired, old)[-MAX_RETIRED_VALUES:]
        masked = {*_current.values(), *_retired}
        _known = tuple(
            sorted(
                (v for v in masked if len(v) >= _MIN_MASKED_LENGTH),
                key=len,
                reverse=True,
            )
        )


def _env_path() -> Path:
    """Return the .env path under the current data directory."""
    return config_store.DATA_DIR / ".env"


def _env_value(name: str) -> str | None:
    """Return the stripped environment value of `name`, None when blank or unset.

    The value is not checked here: `_valid_env_value` does that. Callers that
    only ask whether the environment owns the name use this one. Whatever it
    returns is remembered for masking, valid or not. A value taken out of the
    environment at startup counts as the environment's.
    """
    value = (_captured_environment.get(name) or os.environ.get(name) or "").strip()
    _track(name, "environment", value or None)
    return value or None


def _valid_env_value(name: str) -> str | None:
    """Return the environment value of `name` if it passes the value pattern.

    An invalid value is never returned. The first time it is seen one warning
    names the variable (never the value); the warning is armed again once the
    variable is valid or unset.
    """
    value = _env_value(name)
    if value is not None and _VALUE_PATTERN.fullmatch(value) is not None:
        _warned_invalid_env.discard(name)
        return value
    if value is None:
        _warned_invalid_env.discard(name)
    elif name not in _warned_invalid_env:
        _warned_invalid_env.add(name)
        logger.warning(
            "Environment variable %s is set but invalid (wrong characters or length); "
            "it is ignored",
            name,
        )
    return None


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

    A malformed or invalid entry counts as unset and is reported once by name
    only (armed again once the entry is valid or gone). The last assignment
    wins, as in a shell. A usable value is remembered for masking.

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
        _warned_invalid_file.discard(name)
        _track(name, "file", None)
        return None
    if _VALUE_PATTERN.fullmatch(value) is None:
        _track(name, "file", None)
        if name not in _warned_invalid_file:
            _warned_invalid_file.add(name)
            logger.warning("Ignoring invalid value for %s in the .env file", name)
        return None
    _warned_invalid_file.discard(name)
    _track(name, "file", value)
    return value


def get_secret(name: str) -> str | None:
    """Return the current value of a secret, the environment first.

    Args:
        name: `GITHUB_TOKEN` or `CLOAKBROWSER_LICENSE_KEY`.

    Returns:
        The value, or None when it is set nowhere. A set but invalid
        environment variable gives None as well: the file is not consulted, as
        the environment still owns the name.

    Raises:
        KeyError: `name` is not a recognised secret.
        OSError: The file exists but could not be read.
    """
    _check_name(name)
    if _env_value(name):
        return _valid_env_value(name)
    return _file_value(name)


def secret_source(name: str) -> Source | None:
    """Report where a secret's value comes from.

    Args:
        name: `GITHUB_TOKEN` or `CLOAKBROWSER_LICENSE_KEY`.

    Returns:
        "environment", "environment_invalid" (the variable is set but its value
        is not a valid secret, so it is not used), "file", or None when unset.

    Raises:
        KeyError: `name` is not a recognised secret.
        OSError: The file exists but could not be read.
    """
    _check_name(name)
    if _env_value(name):
        return "environment" if _valid_env_value(name) else "environment_invalid"
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
            raise SecretFromEnvironmentError(
                name, invalid=_valid_env_value(name) is None
            )
        # An interrupted write can leave a temp copy holding both secrets.
        for stale in _env_path().parent.glob("..env-*"):
            stale.unlink(missing_ok=True)
        kept = [line for line in _read_lines() if _line_name(line) != name]
        if value is not None:
            kept.append(f"{name}={value}")
        _set_pending((value,) if value is not None else ())
        try:
            config_store.write_file_atomic(
                _env_path(), "".join(f"{line}\n" for line in kept), mode=0o600
            )
            # In effect from here on, and remembered before the mask is lifted.
            _track(name, "file", value)
        finally:
            _set_pending(())


def _set_pending(values: tuple[str, ...]) -> None:
    """Set the values that are masked only while a write is in progress."""
    global _pending
    _pending = values


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
    validate_secret(value)
    try:
        _rewrite(name, value)
    except SecretFromEnvironmentError:
        raise  # the message is the secret's name
    except OSError as error:
        # The text of a failure can quote the new value (a path or a message),
        # and it is logged later by the caller, after the pending mask is gone.
        # So it is cleaned here, and the original is not chained.
        reason = (error.strerror or type(error).__name__).replace(value, "***")
        raise OSError(error.errno, reason) from None
    except Exception as error:  # noqa: BLE001 - unknown failure: say only its type
        raise RuntimeError(
            f"{type(error).__name__} while storing the secret"
        ) from None


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


def absorb_environment() -> None:
    """Move both secrets out of `os.environ` into memory. Call it once, at startup.

    Afterwards nothing the app starts, library code included, inherits them.
    The store still reports them as coming from the environment, validates them
    and refuses to change them; an invalid or blank variable is removed too.
    The file values are read once so they are masked from the start.

    NOTE: cloakbrowser reads `CLOAKBROWSER_LICENSE_KEY` from `os.environ` by
    itself when it launches the browser. After this call an environment key no
    longer reaches the browser, until the launch passes it explicitly.
    """
    for name in SECRET_NAMES:
        value = os.environ.pop(name, None)
        if value is not None and value.strip():
            _captured_environment[name] = value.strip()
    for name in SECRET_NAMES:
        _env_value(name)
        try:
            _file_value(name)
        except OSError:
            logger.error("Could not read the secrets file at startup")


def redact(text: str) -> str:
    """Replace every secret value this process has seen in `text` with "***".

    Seen means read from the environment or the file, stored, or taken from the
    environment at startup, including values since rotated or cleared. It never
    reads the file, so a read failure cannot unmask anything. Only exact values
    are matched: a value changed on the way (split across two writes, encoded,
    truncated) is not found.

    Args:
        text: Any string, such as an error message about to be logged.

    Returns:
        `text` with the values removed.
    """
    for name in SECRET_NAMES:
        _env_value(name)  # picks up a variable set since the last call
    # Pending first, known second: a value moves from pending to known (known is
    # updated before pending is cleared), so reading in this order never misses
    # a value that is in either list. Longest first over both, so a short value
    # inside a longer one cannot leave the rest of the longer one visible.
    pending = _pending
    known = _known
    values = sorted({*known, *pending}, key=len, reverse=True) if pending else known
    for value in values:
        text = text.replace(value, "***")
    return text


def child_environment() -> dict[str, str]:
    """Return a copy of the process environment without the two secrets.

    Every subprocess the app starts gets this as its environment, so none of
    them inherits a token or key. This is inheritance only, not secrecy: code
    running as the same user can still read DATA_DIR/.env and the parent's
    /proc entries. Everything else (PATH, JAVA_HOME, HOME, TMPDIR, locale) is
    kept. Names are compared case-insensitively.

    Returns:
        A new dict, safe to pass as `env=`.
    """
    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() not in SECRET_NAMES
    }


def _mask(value: object) -> object:
    """Return `value`, or a redacted text if its str or repr shows a secret.

    Numbers, booleans and None are returned as they are. A value that cannot be
    printed is replaced by a fixed text, so nothing is left to leak later.
    """
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return redact(value)
    try:
        text, shown = str(value), repr(value)
    except Exception:  # noqa: BLE001 - arbitrary objects; never let logging fail
        return "<unprintable argument>"
    masked = redact(text)
    if masked != text:
        return masked
    masked_repr = redact(shown)
    return masked_repr if masked_repr != shown else value


def _redact_record(record: logging.LogRecord) -> None:
    """Redact message, arguments, traceback and stack of a record in place.

    The arguments stay arguments (only their values change), because formatters
    such as uvicorn's read `record.args`. The traceback is rendered into
    `exc_text` and `exc_info` is dropped, so no handler can format it raw.
    """
    record.msg = _mask(record.msg)
    if isinstance(record.args, tuple):
        record.args = tuple(_mask(arg) for arg in record.args)
    elif isinstance(record.args, dict):
        record.args = {key: _mask(arg) for key, arg in record.args.items()}
    if record.exc_info and record.exc_info[0] is not None:
        try:
            trace = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = redact(trace.rstrip("\n"))
        except Exception:  # noqa: BLE001 - never let logging fail
            record.exc_text = "<traceback unavailable>"
    record.exc_info = None
    if record.stack_info:
        record.stack_info = redact(record.stack_info)


_log_redaction = threading.local()
_log_redaction_installed = False


def install_log_redaction() -> None:
    """Make every log record from this process carry redacted text.

    Wraps the log record factory, so the message, its arguments and any
    traceback are redacted when the record is created, before a handler (a
    console, a file, a test) sees it. A log call never raises because of it; if
    redaction itself fails the record is replaced by a fixed text. Safe to call
    more than once. Records created while a record is being redacted are left
    alone, which prevents recursion.
    """
    global _log_redaction_installed
    if _log_redaction_installed:
        return
    _log_redaction_installed = True
    previous = logging.getLogRecordFactory()

    def redacting_factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        if getattr(_log_redaction, "active", False):
            return record
        _log_redaction.active = True
        try:
            _redact_record(record)
        except Exception:  # noqa: BLE001 - fail closed: nothing may leak or raise
            record.msg = "<log record dropped: it could not be redacted>"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        finally:
            _log_redaction.active = False
        return record

    logging.setLogRecordFactory(redacting_factory)
