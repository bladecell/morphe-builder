"""Locked, atomic access to config.yaml, the app's only database.

Every change goes through `update_config`: it holds one process-wide lock while it
loads the file, applies a short mutation to those fresh contents and replaces the
file atomically. Never hold the lock (or call `update_config`) around slow work such
as network calls, the scraper or subprocesses; do the work first, then apply a small
mutation with the result.

The lock is per process: this is safe only while the app runs as a single uvicorn
worker. A mutation must not call `update_config` itself; the outer save would
overwrite the inner one.
"""

import copy
import os
import shutil
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT)))

DEFAULT_CONFIG = {
    "repositories": {
        "cli": "MorpheApp/morphe-cli",
        "apkeditor": "REAndroid/APKEditor",
    },
    "sources": [
        {"name": "Morphe Patches", "repo": "MorpheApp/morphe-patches", "active": True}
    ],
    "tools": {
        "cli_jar": "bin/morphe-cli.jar",
        "apkeditor_jar": "bin/APKEditor.jar",
    },
    "apps": [],
    "settings": {
        "server_url": "http://localhost:8000",
        "cron_schedule": "0 */4 * * *",
        "notification_urls": [],
    },
}

# Reentrant: seeding a missing file inside `update_config` takes the lock again.
_CONFIG_LOCK = threading.RLock()


def write_file_atomic(path: Path, text: str, mode: int | None = None) -> None:
    """Replace the file at `path` with `text` so readers never see a partial file.

    Args:
        path: Destination file; its directory is created if missing.
        text: Full new file contents.
        mode: Permission bits for the result. When None, an existing file keeps
            its mode and a new file gets 0644.

    Raises:
        OSError: The temporary file could not be written or moved into place.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(text)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        if mode is not None:
            os.chmod(tmp_name, mode)
        elif path.exists():
            shutil.copymode(path, tmp_name)
        else:
            os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _write_atomic(text: str) -> None:
    """Replace config.yaml with `text` atomically.

    Args:
        text: Full new file contents.

    Raises:
        OSError: The temporary file could not be written or moved into place.
    """
    write_file_atomic(DATA_DIR / "config.yaml", text)


def save_config(config_data: dict) -> None:
    """Atomically write `config_data` as config.yaml.

    Prefer `update_config` for changes: this overwrites whatever is on disk.

    Args:
        config_data: Complete config to store.

    Raises:
        OSError: The file could not be written.
    """
    with _CONFIG_LOCK:
        _write_atomic(yaml.dump(config_data, sort_keys=False))


def _seed_if_missing() -> None:
    """Create config.yaml from the project root copy, else from the defaults."""
    config_path = DATA_DIR / "config.yaml"
    with _CONFIG_LOCK:
        if config_path.exists():
            return
        source_config = PROJECT_ROOT / "config.yaml"
        if source_config.exists():
            _write_atomic(source_config.read_text())
            print(f"[Init] Seeded {config_path} from {source_config}")
        else:
            save_config(DEFAULT_CONFIG)
            print(f"[Init] Generated default {config_path}")


def load_config() -> dict:
    """Read config.yaml, creating it on first run.

    Returns:
        The parsed config. Mutating it does not change the file.

    Raises:
        OSError: The file could not be read or created.
        yaml.YAMLError: The file is not valid YAML.
    """
    config_path = DATA_DIR / "config.yaml"
    if not config_path.exists():
        _seed_if_missing()
    # Writes replace the file atomically, so no lock is needed to read it.
    with open(config_path, "r") as file:
        return yaml.safe_load(file)


def update_config(mutate: Callable[[dict], None]) -> dict:
    """Apply `mutate` to the current config and save the result atomically.

    The whole load-mutate-save runs under the config lock. If `mutate` raises,
    nothing is saved and the exception propagates. The file is only rewritten when
    `mutate` changed something.

    Args:
        mutate: Short, non-blocking function that edits the loaded config in place.

    Returns:
        The config as it is on disk after the update.

    Raises:
        OSError: The file could not be read or written.
        Exception: Whatever `mutate` raises, unchanged.
    """
    with _CONFIG_LOCK:
        config = load_config()
        original = copy.deepcopy(config)
        mutate(config)
        if config != original:
            save_config(config)
        return config
