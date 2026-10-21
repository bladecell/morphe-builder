from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    BackgroundTasks,
    UploadFile,
    File,
    Form,
)
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
import hashlib
import shutil
import json
import math
import os
import re
import sys
import io
import time
import apprise
from apprise import plugins as apprise_plugins
from dataclasses import dataclass
from typing import Any, Callable, List, Protocol, TypeVar, cast
from pathlib import Path
from urllib.parse import unquote
from src.patcher import (
    get_supported_versions,
    load_patch_list,
    PatchListError,
    PatchSelectionError,
    VersionLookupError,
    get_all_available_apps,
    find_file_collision,
    get_source_paths,
    normalize_repo,
    plan_source_sync,
    update_tools,
    get_current_version,
    check_dependencies,
    check_and_download_release,
    load_local_versions,
    selected_patches,
    active_selected_repos,
    is_base_repo,
    DATA_DIR,
    GitHubRateLimitError,
    GitHubTokenRejectedError,
    github_token_permissions,
    github_token_warning,
    github_token_rejected,
    SYNC_LOCK,
    remove_stale_download_files,
    compatible_patches as get_compatible_patches,
    resolve_patch_selection,
    source_path_alias,
    is_valid_repo,
)
from packaging import version
from apscheduler.jobstores.base import JobLookupError  # type: ignore[import-untyped]
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import threading
import uuid
import yaml  # type: ignore[import-untyped]
import asyncio
import logging
import queue
from src import secrets_store
from src import request_guards
from src.request_guards import RequestGuardMiddleware
from src.config_store import load_config, update_config

logger = logging.getLogger(__name__)
secrets_store.install_log_redaction()
T = TypeVar("T")

# Silence verbose Uvicorn access logs
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

PROJECT_ROOT = Path(__file__).parent.parent
app = FastAPI()
scheduler = BackgroundScheduler()


class ScraperUnavailableError(RuntimeError):
    """The scraper browser cannot serve a task (launch failed or shutting down)."""


LAUNCH_COOLDOWN_SECONDS = 60.0
INITIAL_LAUNCH_WAIT_SECONDS = 30
SHUTDOWN_JOIN_SECONDS = 15
LIVENESS_POLL_SECONDS = 0.5


class Downloader(Protocol):
    """What the worker needs from the browser wrapper."""

    def close(self) -> None:
        """Close the browser."""


@dataclass
class _Task:
    """One unit of work for the worker thread and the queue its answer goes to."""

    func: Callable[..., object]
    args: tuple[object, ...]
    kwargs: dict[str, object]
    result: "queue.Queue[tuple[object, Exception | None]]"


def _launch_downloader() -> Downloader:
    """Start the persistent APKMirror downloader (launches the browser).

    Returns:
        A new `ApkMirror_Downloader`.
    """
    from src.patcher import ApkMirror_Downloader

    return ApkMirror_Downloader()


# Global worker to manage a persistent downloader instance
class ScraperWorker(threading.Thread):
    """Owns the one browser; every scrape and build runs in this thread.

    A failed launch does not end the thread: the next task after a cooldown gets
    another attempt. Until then tasks fail fast with `ScraperUnavailableError`.
    No caller is ever left waiting on a dead worker: whatever ends the thread,
    queued and later tasks are answered with `ScraperUnavailableError`.
    """

    def __init__(
        self,
        launch: Callable[..., Downloader] = _launch_downloader,
        clock: Callable[[], float] = time.monotonic,
        cooldown: float = LAUNCH_COOLDOWN_SECONDS,
    ) -> None:
        """Create the worker; call `start()` to launch the browser.

        Args:
            launch: Factory returning a downloader (with `close()`).
            clock: Monotonic seconds source; the cooldown is measured with it.
            cooldown: Seconds to wait after a failed launch before retrying.
        """
        super().__init__(daemon=True, name="ScraperWorker")
        self.task_queue: queue.Queue[_Task | None] = queue.Queue()  # None = STOP
        self.downloader: Downloader | None = None
        self.error: str | None = None  # redacted text of the last launch failure
        self.initialized = threading.Event()
        self._launch = launch
        self._clock = clock
        self._cooldown = cooldown
        self._next_attempt_at = 0.0
        self._launching = False  # True while `_launch` runs
        self._busy_since: float | None = None  # epoch seconds; None when idle
        # Guards `_stopping` together with the queue put, so that no task can be
        # queued after the STOP/drain: execute and shutdown cannot interleave.
        self._lock = threading.Lock()
        self._stopping = False

    def run(self) -> None:
        """Thread body: launch once, serve tasks until STOP, then clean up."""
        # Playwright Sync API requires NO event loop in the thread
        asyncio.set_event_loop(None)
        try:
            self._try_launch()
            self.initialized.set()
            while (task := self.task_queue.get()) is not None:
                self._run_task(task)
        finally:
            self._finish()

    def _run_task(self, task: _Task) -> None:
        """Run one task and hand its result or error to the waiting caller."""
        self._busy_since = time.time()
        try:
            self._run_task_body(task)
        finally:
            self._busy_since = None

    def _run_task_body(self, task: _Task) -> None:
        """Launch the browser if needed, run the task and answer its caller."""
        try:
            if self.downloader is None and self._clock() >= self._next_attempt_at:
                self._try_launch()
            if self.downloader is None:
                raise ScraperUnavailableError(self._unavailable_message())
            # Pass downloader as a keyword argument to keep positional args clean
            result = task.func(*task.args, scraper=self.downloader, **task.kwargs)
            task.result.put((result, None))
        except Exception as error:  # noqa: BLE001 - handed to the caller
            task.result.put((None, error))
        except BaseException as error:  # noqa: BLE001 - SystemExit and the like
            # Must not end the thread or leave the caller waiting.
            wrapped = RuntimeError(f"Scraper task ended with {type(error).__name__}")
            wrapped.__cause__ = error
            task.result.put((None, wrapped))

    def _try_launch(self) -> None:
        """Launch the browser; on failure record the redacted error and cooldown."""
        logger.info("Initializing persistent ApkMirror_Downloader...")
        self._launching = True
        try:
            self.downloader = self._launch()
        except Exception as error:  # noqa: BLE001 - any launch failure is recoverable
            # No exc_info: the traceback would repeat the unredacted message.
            self.error = secrets_store.redact(f"{type(error).__name__}: {error}")
            self._next_attempt_at = self._clock() + self._cooldown
            logger.error("Browser launch failed: %s", self.error)
            return
        finally:
            self._launching = False
        self.error = None

    def _finish(self) -> None:
        """Close the browser and answer every task still queued."""
        with self._lock:
            self._stopping = True  # no task can be queued after this
        self.initialized.set()
        try:
            self._close_downloader()
        finally:
            self._answer_queued_tasks()

    def _answer_queued_tasks(self) -> None:
        """Answer every task still queued with `ScraperUnavailableError`."""
        while True:
            try:
                task = self.task_queue.get_nowait()
            except queue.Empty:
                return
            if task is not None:
                task.result.put((None, ScraperUnavailableError("Scraper stopped")))

    def _close_downloader(self) -> None:
        """Close the browser if there is one; a failing close is logged."""
        if self.downloader is None:
            return
        try:
            self.downloader.close()
        except Exception as error:  # noqa: BLE001 - see below
            # Deliberately broad: this runs at the thread boundary. Any error
            # escaping here would skip the queue drain, end the thread through
            # `threading.excepthook` and print an unredacted traceback outside
            # the logger. The error is logged (redacted), never swallowed.
            logger.error(
                "Closing the browser failed: %s", secrets_store.redact(str(error))
            )
        self.downloader = None

    def _retry_in(self) -> int | None:
        """Whole seconds until the next launch attempt, or None when not waiting."""
        if self.downloader is not None or self._stopping or self._launching:
            return None
        return max(0, math.ceil(self._next_attempt_at - self._clock()))

    def _unavailable_message(self) -> str:
        """Text for a task refused because the browser is down."""
        return f"Browser unavailable ({self.error}); it is retried after a cooldown"

    def status(self) -> dict[str, str | int | float | None]:
        """Report the browser state for the UI.

        Returns:
            `state` ("starting" during the first launch and any relaunch,
            "available", "unavailable", "stopping" while work still runs after
            a stop request, or "stopped" once the thread has ended), the
            redacted last launch `error` (or None), `retry_in` seconds until the
            next launch attempt (or None) and `busy_since`, the epoch seconds at
            which the running task started (or None when idle). A `busy_since`
            far in the past with "available" is a stuck task.
        """
        if self._stopping:
            state = "stopping" if self.is_alive() else "stopped"
        elif self.downloader is not None:
            state = "available"
        elif self._launching or not self.initialized.is_set():
            state = "starting"
        else:
            state = "unavailable"
        return {
            "state": state,
            "error": self.error,
            "retry_in": self._retry_in(),
            "busy_since": self._busy_since,
        }

    def execute(self, func: Callable[..., T], *args: object, **kwargs: object) -> T:
        """Run a function in the worker thread and return its result.

        Waits for the result with no timeout while the worker is alive: a timeout
        would free the caller (and the update lock) while the worker is still
        busy and let more work queue behind it.

        Args:
            func: Callable taking the downloader as the `scraper` keyword.
            *args: Positional arguments for `func`.
            **kwargs: Keyword arguments for `func`.

        Returns:
            Whatever `func` returns.

        Raises:
            ScraperUnavailableError: The browser is down (launch failed, cooling
                down), the worker is stopped or its thread has died.
            Exception: Whatever `func` raised.
        """
        if not self.initialized.is_set():
            self.initialized.wait(timeout=INITIAL_LAUNCH_WAIT_SECONDS)

        result: queue.Queue[tuple[object, Exception | None]] = queue.Queue()
        with self._lock:
            if self._stopping:
                raise ScraperUnavailableError("Scraper stopped")
            self.task_queue.put(_Task(func, args, kwargs, result))
        value, error = self._wait_for(result)
        if error is not None:
            raise error
        return cast(T, value)

    def _wait_for(
        self, result: "queue.Queue[tuple[object, Exception | None]]"
    ) -> tuple[object, Exception | None]:
        """Wait for a task's answer; give up only when the thread is gone."""
        while True:
            try:
                return result.get(timeout=LIVENESS_POLL_SECONDS)
            except queue.Empty:
                if self.is_alive():
                    continue
            try:
                return result.get_nowait()  # answered just before the thread ended
            except queue.Empty:
                raise ScraperUnavailableError("Scraper thread is not running") from None

    def shutdown(self) -> None:
        """Ask the thread to stop after the task it is running, and close the browser.

        A task already running (for example a build) is not cancelled: the stop
        request queues behind it, the task finishes, then the browser is closed.
        Tasks queued behind the stop are answered with `ScraperUnavailableError`.
        """
        with self._lock:
            self._stopping = True
            self.task_queue.put(None)


SCRAPER_WORKER = ScraperWorker()
# True while any scraper activity (a version refresh or a build) is queued or
# running. It is derived from `_ACTIVITY_COUNT`, so one activity ending cannot
# clear it while another still runs.
SCRAPER_ACTIVE = False
_ACTIVITY_COUNT = 0
# Guards the build flag, the activity count, the sync flag and the update cache
# generation. Never held across a network call or a worker wait.
_STATE_LOCK = threading.Lock()

app.add_middleware(RequestGuardMiddleware)


@app.exception_handler(StarletteHTTPException)
async def redacted_http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> Response:
    """Answer an HTTP error like the default handler, with secrets removed.

    Error details are often built from exception text (429, 502), so this is
    the one place that keeps a secret value out of every one of them.

    Args:
        request: The request that failed.
        exc: The error to answer.

    Returns:
        The default JSON error response, with a text `detail` redacted.
    """
    if isinstance(exc.detail, str):
        exc = StarletteHTTPException(
            exc.status_code, secrets_store.redact(exc.detail), exc.headers
        )
    return await http_exception_handler(request, exc)


# No CORS middleware on purpose: the UI is served by this app (same origin) and
# Obtainium is not a browser, so neither needs CORS headers. Without them a
# browser blocks other sites from reading responses or sending JSON PUT/DELETE.

# Setup templates directory
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "src" / "templates"))

# Self-hosted page assets: the built Tailwind stylesheet and vendored Alpine.
STATIC_DIR = PROJECT_ROOT / "src" / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def asset_url(filename: str) -> str:
    """Return the URL of a static file, tagged with a hash of its content.

    The app sends no Cache-Control for static files, so a browser may reuse an old
    copy; the changing query string makes it fetch a rebuilt stylesheet.

    Args:
        filename: Name of a file directly under `src/static/`.

    Returns:
        `/static/<filename>?v=<first 12 hex of the SHA-256 of the file>`. If the
        file is missing, the plain `/static/<filename>` and an error is logged, so
        the admin page still renders (unstyled) instead of answering 500.
    """
    path = STATIC_DIR / filename
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        logger.error(
            "Static file %s is missing; rebuild the stylesheet with: "
            "uv run python scripts/build_css.py",
            path,
        )
        return f"/static/{filename}"
    return f"/static/{filename}?v={digest[:12]}"


templates.env.globals["asset_url"] = asset_url

# Mount static icons directory
icons_dir = DATA_DIR / "apks" / "icons"
icons_dir.mkdir(parents=True, exist_ok=True)
app.mount("/icons", StaticFiles(directory=str(icons_dir)), name="icons")

BUILD_STATUS = {"in_progress": False, "message": "Idle", "last_run": "Never"}
DIAGNOSTICS = {"java": False, "scraper": False}
LOG_PATH = DATA_DIR / "bin" / "build.log"
TOOLS_UPDATING = False
SYNC_ERROR: str | None = None  # Last background sync failure shown in the UI, or None


def ensure_data_dirs():
    """Ensure all required subdirectories exist in the DATA_DIR."""
    (DATA_DIR / "bin").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "apks" / "raw").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "apks" / "patched").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "apks" / "icons").mkdir(parents=True, exist_ok=True)


def remove_legacy_cloakbrowser_cache() -> None:
    """Delete the retired cloakbrowser binary cache (and any key file in it)."""
    shutil.rmtree(DATA_DIR / "bin" / "cloakbrowser", ignore_errors=True)


# Cache for update status to prevent excessive polling load
UPDATE_CACHE: dict[str, Any] = {"timestamp": 0, "results": None}
UPDATE_LOCK = threading.Lock()
# Bumped on every invalidation. A refresh remembers the value it started with and
# stamps its result fresh only if it is unchanged when the refresh ends.
_UPDATE_GENERATION = 0


def _invalidate_updates() -> None:
    """Mark the cached update status as out of date, including a running refresh."""
    global _UPDATE_GENERATION
    with _STATE_LOCK:
        _UPDATE_GENERATION += 1
        UPDATE_CACHE["timestamp"] = 0


@app.get("/api/ui/updates")
def check_updates() -> dict:
    """Check each pipeline app for updates against remote sources.

    Returns:
        The update status per app id: the cached result while it is under 60
        seconds old, the last (stale) result or {} while a build, another
        scraper activity or another refresh is running, else a fresh one.
    """
    now = time.time()

    # Return cached results if they are less than 60 seconds old
    if UPDATE_CACHE["results"] and (now - UPDATE_CACHE["timestamp"] < 60):
        return UPDATE_CACHE["results"]

    # Avoid enqueueing behind a long-running build: return stale cached
    # results immediately (the next poll after the build finishes will refresh).
    if BUILD_STATUS.get("in_progress") or SCRAPER_ACTIVE:
        return _stale_updates()

    # One refresh at a time: a request that arrives during one gets the stale
    # result (or {}) at once instead of queueing more scraper work behind it.
    if not UPDATE_LOCK.acquire(blocking=False):
        return _stale_updates()
    try:
        # A refresh may have finished between the freshness check and the lock.
        if UPDATE_CACHE["results"] and (time.time() - UPDATE_CACHE["timestamp"] < 60):
            return UPDATE_CACHE["results"]
        return _refresh_updates()
    finally:
        UPDATE_LOCK.release()


def _stale_updates() -> dict:
    """Return the last computed update status, or {} when there is none yet."""
    results = UPDATE_CACHE["results"]
    return results if results is not None else {}


class _OfflineScraper:
    """Stand-in used while the browser is down: every lookup fails, so
    `get_supported_versions` falls back to its cached versions."""

    def latest_version(self, package_name: str) -> str:
        """Always fail, as the browser is unavailable."""
        raise ScraperUnavailableError("Browser unavailable")


def _cached_versions(package_id: str, config: dict) -> list[str]:
    """Versions known without the browser: patch-derived or cached ones.

    Args:
        package_id: Android package id.
        config: The loaded config.

    Returns:
        The versions, or [] when only the browser could tell.
    """
    versions, _, _ = get_supported_versions(
        package_id, config, scraper=_OfflineScraper(), quiet=True
    )
    return list(versions)


def _degraded_versions(
    package_id: str, config: dict, error: Exception
) -> tuple[list[str], str | None]:
    """Answer for an app whose live lookup failed.

    Args:
        package_id: Android package id.
        config: The loaded config.
        error: Why the live lookup failed.

    Returns:
        (cached versions, None) when there are some, else ([], message for the UI).
        An unreadable patch list is reported instead, as `PatchListError` is
        elsewhere.
    """
    try:
        cached = _cached_versions(package_id, config)
    except PatchListError as patch_list_error:
        return [], str(patch_list_error)
    if cached:
        return cached, None
    message = secrets_store.redact(str(error))
    if isinstance(error, VersionLookupError):
        logger.error("Version lookup for %s failed: %s", package_id, message)
        message = f"Version lookup failed: {message}"
    return [], message


def _redacted_text(text: str | None) -> str | None:
    """Return `text` with secret values replaced, or None for None."""
    return None if text is None else secrets_store.redact(text)


def _redacted(error: BaseException) -> str:
    """Error text safe to log and show: secret values replaced."""
    return secrets_store.redact(f"{type(error).__name__}: {error}")


def _refresh_updates() -> dict:
    """Compute the update status of every pipeline app and cache it.

    Called with `UPDATE_LOCK` held. The result is stamped fresh only when no
    invalidation happened while it was being computed; otherwise it is kept as
    the stale fallback with timestamp 0 and the next poll refreshes again.

    Returns:
        The update status per app id.
    """
    global UPDATE_CACHE
    with _STATE_LOCK:
        started_generation = _UPDATE_GENERATION
    config = load_config()
    update_status = {}

    # Check which apps actually have patched files existing
    patched_dir = DATA_DIR / "apks" / "patched"
    icons_dir = DATA_DIR / "apks" / "icons"

    # Get current versions of all active patch sources
    from src.patcher import get_patch_versions

    remote_patch_versions = get_patch_versions(config)

    for app_info in config.get("apps", []):
        package_id = app_info["id"]

        # Pinned version check
        pinned_version = app_info.get("version")
        patch_list_error = None
        try:
            if pinned_version:
                # Skip APKMirror completely if pinned.
                # get_supported_versions will return the pinned version via JSON check
                remote_versions, _, _ = get_supported_versions(
                    package_id, config, quiet=True
                )
            else:
                # For unpinned apps, use the worker to find the newest from APKMirror
                # We use quiet=True here to avoid log spam from polling
                remote_versions, _, _ = SCRAPER_WORKER.execute(
                    get_supported_versions,
                    package_id,
                    config,
                    quiet=True,
                    raise_lookup_errors=True,
                )
        except PatchListError as error:
            # Not logged: this runs on every poll. The builds and the hourly
            # refresh log it, and the result below carries it to the UI.
            remote_versions = []
            patch_list_error = str(error)
        except (ScraperUnavailableError, VersionLookupError) as error:
            # The browser is down or the lookup failed. Serve what needs no
            # browser (cached versions); an app with no answer carries the error.
            remote_versions, patch_list_error = _degraded_versions(
                package_id, config, error
            )
        except Exception as error:  # noqa: BLE001 - one app must not fail the poll
            remote_versions = []
            patch_list_error = f"Version lookup failed: {_redacted(error)}"
            logger.error("Update check for %s: %s", package_id, patch_list_error)

        # Check if file exists (glob for versioned name or legacy)
        file_exists = False
        if list(patched_dir.glob(f"{package_id}-*.apk")):
            file_exists = True

        icon_exists = (icons_dir / f"{package_id}.png").exists()

        # The 'latest' for update purposes is the first item in the list
        latest_remote_app_str = remote_versions[0] if remote_versions else "Unknown"

        # Use config for current state
        last_build = app_info.get("last_successful_build", {})
        current_patched_app_str = last_build.get("app_version", "None")

        has_update = False
        # Update needed if:
        # 1. Remote app version > current patched app version
        # 2. Remote patch versions != our last used patch versions for this app
        # 3. File doesn't exist
        # 4. Patch selection differs from the one the last build used

        if latest_remote_app_str != "Unknown" and current_patched_app_str != "None":
            try:
                latest = version.parse(latest_remote_app_str)
                if latest > version.parse(current_patched_app_str):
                    has_update = True
            except version.InvalidVersion:
                if latest_remote_app_str != current_patched_app_str:
                    has_update = True
        elif current_patched_app_str == "None" and latest_remote_app_str != "Unknown":
            has_update = True

        # Compare only the repos that were used in the last build
        used_repos = last_build.get("patch_versions", {}).keys()
        if not used_repos and not has_update:
            has_update = True
        else:
            for repo in used_repos:
                built_versions = last_build.get("patch_versions", {})
                if remote_patch_versions.get(repo) != built_versions.get(repo):
                    has_update = True
                    break

        if not file_exists:
            has_update = True

        if last_build:
            try:
                if last_build.get("patches") != selected_patches(app_info, config):
                    has_update = True
                built_repos = {
                    repo.casefold() for repo in last_build.get("patch_versions", {})
                }
                if not active_selected_repos(app_info, config).issubset(built_repos):
                    has_update = True
            except PatchSelectionError:
                has_update = True
                patch_list_error = "Invalid patch selection configuration"

        update_status[package_id] = {
            "has_update": has_update,
            "latest": latest_remote_app_str,
            "current": current_patched_app_str,
            "file_exists": file_exists,
            "icon_exists": icon_exists,
            "status": (
                "Error" if patch_list_error else app_info.get("status", "Unknown")
            ),
            "error": _redacted_text(patch_list_error or app_info.get("error")),
        }

    # Stamped when the refresh ends: a slow one must not store an expired result.
    with _STATE_LOCK:
        is_current = started_generation == _UPDATE_GENERATION
        UPDATE_CACHE = {
            "timestamp": time.time() if is_current else 0,
            "results": update_status,
        }
    return update_status


# --- WEB UI ROUTES ---


@app.get("/", response_class=HTMLResponse)
async def web_ui(request: Request):
    """Serves the main Web UI with the initial data injected."""
    config = load_config()
    settings = config.get("settings")
    if isinstance(settings, dict):
        settings.pop("notification_urls", None)
    available_apps = get_all_available_apps(config)

    metadata = {}
    metadata_file = DATA_DIR / "bin" / "metadata.json"
    if metadata_file.exists():
        with open(metadata_file, "r") as f:
            try:
                metadata = json.load(f)
            except:
                pass

    # This dictionary MUST match the variables used in your index.html
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "config": config,
            "available_apps": available_apps,
            "metadata": metadata,
            "environment_settings": {
                "server_url": "SERVER_URL" in os.environ,
                "cron_schedule": "CRON_SCHEDULE" in os.environ,
            },
        },
    )


@app.get("/api/ui/patches/{package_id}")
def get_patches_for_app(package_id: str, format: str | None = None):
    """List the patches of all active sources that apply to one package.

    Args:
        package_id: Android package id.

    Returns:
        One dict per patch with name, description, enabled_by_default, the display
        name of its source under `source` and `is_base` (the source is the base
        source, decided by repo).

    Raises:
        HTTPException: 502, with the repo in the detail, when an active source's
            patch list is unreadable.
    """
    config = load_config()
    if format == "grouped":
        return _get_grouped_patches_for_app(package_id, config)
    if format is not None:
        raise HTTPException(status_code=400, detail="Unsupported patch catalog format")

    compatible_patches: list[dict[str, object]] = []

    for source in config.get("sources", []):
        if not source.get("active", True):
            continue

        json_path = get_source_paths(source["repo"])[".json"]
        if not json_path.exists():
            continue

        try:
            patches_list = load_patch_list(source["repo"])
        except PatchListError as error:
            logger.error("Patch dialog for %s: %s", package_id, error)
            raise HTTPException(status_code=502, detail=str(error)) from error
        for patch in patches_list:
            compat_pkgs = patch.get("compatiblePackages")
            use = False
            if isinstance(compat_pkgs, dict) and package_id in compat_pkgs:
                use = True
            elif isinstance(compat_pkgs, list):
                for pkg in compat_pkgs:
                    if not isinstance(pkg, dict):
                        continue
                    # ReVanced schema uses packageName, others use name
                    if (
                        pkg.get("packageName") == package_id
                        or pkg.get("name") == package_id
                    ):
                        use = True
                        break
            elif not compat_pkgs:
                use = True

            if use:
                # Determine if it's enabled by default (check various schema fields)
                is_default = patch.get(
                    "default", patch.get("enabled_by_default", patch.get("use", True))
                )

                # Avoid duplicates from different sources
                if not any(p["name"] == patch.get("name") for p in compatible_patches):
                    compatible_patches.append(
                        {
                            "name": patch.get("name"),
                            "description": patch.get(
                                "description", "No description available."
                            ),
                            "enabled_by_default": is_default,
                            "source": source["name"],
                            "is_base": is_base_repo(source["repo"]),
                        }
                    )
    return compatible_patches


def _grouped_patch_entry(
    patch: dict, source: dict, selected: bool
) -> dict[str, object]:
    """Return the existing patch-dialog fields plus an explicit selection flag."""
    return {
        "name": patch.get("name"),
        "description": patch.get("description", "No description available."),
        "enabled_by_default": patch.get(
            "default", patch.get("enabled_by_default", patch.get("use", True))
        ),
        "source": source["name"],
        "is_base": is_base_repo(source["repo"]),
        "selected": selected,
    }


def _stored_patch_entry(
    name: str, repo: str, source: str, is_base: bool, status: str
) -> dict[str, object]:
    """Represent a stored selection whose patch is absent from the catalog."""
    return {
        "name": name,
        "description": "Stored selection is not currently available.",
        "enabled_by_default": False,
        "source": source,
        "is_base": is_base,
        "selected": True,
        "status": status,
    }


def _get_grouped_patches_for_app(package_id: str, config: dict) -> dict[str, object]:
    """Build the opt-in grouped catalog and expose stored/resolved selection state."""
    app = next(
        (entry for entry in config.get("apps", []) if entry.get("id") == package_id),
        {"id": package_id, "include_patches": []},
    )
    stored = app.get("include_patches") or []
    selected_by_repo: dict[str, dict[str, str]] = {}
    stored_repo_names: dict[str, str] = {}
    if isinstance(stored, dict):
        for repo, names in stored.items():
            if isinstance(repo, str) and isinstance(names, list):
                selected_by_repo[repo.casefold()] = {
                    name.casefold(): name for name in names if isinstance(name, str)
                }
                stored_repo_names[repo.casefold()] = repo

    resolved: dict[str, list[str]] = {}
    preview_errors: list[tuple[str, list[str]]] = []
    selection_error: dict[str, str] | None = None
    try:
        resolved = resolve_patch_selection(
            app,
            config,
            require_all=False,
            allow_unresolved=True,
            warn_unavailable=False,
            partial_preview=True,
            preview_errors=preview_errors,
        )
    except PatchListError:
        selection_error = {
            "code": "source_unavailable",
            "message": "A selected patch source is unavailable.",
        }
    except PatchSelectionError:
        selection_error = {
            "code": "selection_invalid",
            "message": "Some selected patches cannot be resolved.",
        }
    if preview_errors:
        selection_error = {
            "code": "source_unavailable",
            "message": "A selected patch source is unavailable.",
        }
    resolved_selection = {
        repo.casefold(): {name.casefold() for name in names}
        for repo, names in resolved.items()
    }

    groups: list[dict[str, object]] = []
    source_status: dict[str, str] = {}
    source_by_repo: dict[str, dict] = {}
    seen_repos: set[str] = set()
    for source in config.get("sources", []):
        repo = source.get("repo")
        valid_repo = is_valid_repo(repo)
        repo_key = repo.casefold() if valid_repo else ""
        display_name = source.get("name") if isinstance(source.get("name"), str) else ""
        group: dict[str, object] = {
            "repo": repo if isinstance(repo, str) else "",
            "source": display_name,
            "is_base": is_base_repo(repo) if valid_repo else False,
            "status": "invalid",
            "patches": [],
        }
        catalog_records: list[dict[str, object]] = []
        if valid_repo:
            source_by_repo[repo_key] = source
            if repo_key in seen_repos:
                group["status"] = "conflict"
            elif not source.get("active", True):
                group["status"] = "inactive"
            else:
                seen_repos.add(repo_key)
                alias = source_path_alias(repo, config.get("sources", []))
                paths = get_source_paths(repo)
                records: list[dict] = []
                if alias is not None:
                    group["status"] = "conflict"
                elif paths[".json"].exists():
                    try:
                        records = get_compatible_patches(repo, package_id)
                        group["status"] = (
                            "available" if paths[".mpp"].is_file() else "missing"
                        )
                    except PatchListError:
                        group["status"] = "unreadable"
                else:
                    group["status"] = "missing"

                name_counts: dict[str, int] = {}
                for patch_record in records:
                    name = patch_record.get("name")
                    if isinstance(name, str):
                        folded = name.casefold()
                        name_counts[folded] = name_counts.get(folded, 0) + 1
                if any(count > 1 for count in name_counts.values()):
                    group["status"] = "ambiguous"
                catalog_records = [
                    _grouped_patch_entry(
                        patch_record,
                        source,
                        isinstance(patch_record.get("name"), str)
                        and patch_record["name"].casefold()
                        in resolved_selection.get(repo_key, set()),
                    )
                    for patch_record in records
                ]

            selected_names = selected_by_repo.get(repo_key, {})
            represented = {
                str(entry["name"]).casefold()
                for entry in catalog_records
                if isinstance(entry.get("name"), str)
            }
            catalog_records.extend(
                _stored_patch_entry(
                    selected_name,
                    repo,
                    display_name or repo,
                    is_base_repo(repo),
                    str(group["status"]),
                )
                for folded_name, selected_name in sorted(selected_names.items())
                if folded_name not in represented
            )
            source_status[repo_key] = str(group["status"])
        group["patches"] = catalog_records
        groups.append(group)

    for repo_key, names in selected_by_repo.items():
        if repo_key in source_by_repo:
            continue
        stored_repo = stored_repo_names.get(repo_key, repo_key)
        group = {
            "repo": stored_repo,
            "source": stored_repo,
            "is_base": is_base_repo(stored_repo),
            "status": "deleted",
            "patches": [
                _stored_patch_entry(
                    name, stored_repo, stored_repo, is_base_repo(stored_repo), "deleted"
                )
                for name in sorted(names.values(), key=str.casefold)
            ],
        }
        groups.append(group)
        source_status[repo_key] = "deleted"

    resolved_names = {
        (repo.casefold(), name.casefold())
        for repo, names in resolved.items()
        for name in names
    }
    unresolved: list[dict[str, str | None]] = []
    if isinstance(stored, dict):
        for repo, names in stored.items():
            if not isinstance(repo, str) or not isinstance(names, list):
                continue
            canonical = next(
                (
                    source["repo"]
                    for source in config.get("sources", [])
                    if is_valid_repo(source.get("repo"))
                    and source["repo"].casefold() == repo.casefold()
                ),
                repo,
            )
            status = source_status.get(repo.casefold(), "deleted")
            for name in names:
                if (
                    not isinstance(name, str)
                    or (canonical.casefold(), name.casefold()) in resolved_names
                ):
                    continue
                reason = (
                    "source_unavailable"
                    if status
                    in {"inactive", "deleted", "missing", "unreadable", "conflict"}
                    else "patch_unavailable"
                )
                unresolved.append({"repo": canonical, "name": name, "reason": reason})
    elif isinstance(stored, list):
        assigned = {name.casefold() for _, name in resolved_names}
        blocked_legacy_names = {
            name.casefold() for _, names in preview_errors for name in names
        }
        unresolved.extend(
            {
                "repo": None,
                "name": name,
                "reason": (
                    "source_unavailable"
                    if isinstance(name, str) and name.casefold() in blocked_legacy_names
                    else "patch_unavailable"
                ),
            }
            for name in stored
            if isinstance(name, str) and name.casefold() not in assigned
        )

    has_selection = bool(stored)
    status = (
        "empty"
        if not has_selection
        else "resolved"
        if not unresolved and selection_error is None
        else "partial"
    )
    if unresolved and selection_error is None:
        selection_error = {
            "code": "selection_partial",
            "message": "Some selected patches are unavailable; review the selection.",
        }
    return {
        "sources": groups,
        "selection": {
            "stored": stored,
            "resolved": resolved,
            "status": status,
            "unresolved": unresolved,
            "error": selection_error,
        },
    }


class AddAppPayload(BaseModel):
    package_id: str
    name: str
    include_patches: Any


@app.post("/api/ui/add_app")
def add_app_to_config(payload: AddAppPayload) -> dict[str, str]:
    """Add an app to the pipeline, or edit it when its package id exists.

    Args:
        payload: Package id, display name and selected patch names.

    Returns:
        `{"status": "success"}`.

    Raises:
        HTTPException: 400 when the package id is not a valid Android package id.
    """
    if not PACKAGE_ID_PATTERN.fullmatch(payload.package_id):
        raise HTTPException(status_code=400, detail="Invalid package id")

    if not isinstance(payload.include_patches, (list, dict)):
        raise HTTPException(status_code=400, detail="Invalid patch selection")

    initially_existing = any(
        app.get("id") == payload.package_id for app in load_config().get("apps", [])
    )

    def apply(config: dict) -> None:
        existing = next(
            (a for a in config.get("apps", []) if a["id"] == payload.package_id), None
        )
        if initially_existing and existing is None:
            raise HTTPException(status_code=404, detail="App no longer exists")
        includes = payload.include_patches
        if isinstance(includes, dict):
            canonical_sources: dict[str, dict] = {}
            for source in config.get("sources", []):
                repo = source.get("repo")
                if is_valid_repo(repo):
                    key = repo.casefold()
                    if key in canonical_sources:
                        raise HTTPException(400, "Invalid patch selection")
                    canonical_sources[key] = source
            normalized: dict[str, list[str]] = {}
            normalized_keys: set[str] = set()
            try:
                for repo, names in includes.items():
                    if not is_valid_repo(repo) or not isinstance(names, list):
                        raise ValueError
                    key = repo.casefold()
                    source = canonical_sources.get(key)
                    canonical = source["repo"] if source else repo
                    if canonical.casefold() in normalized_keys:
                        raise ValueError
                    normalized_keys.add(canonical.casefold())
                    if not all(
                        isinstance(name, str) and name.strip() for name in names
                    ):
                        raise ValueError
                    accepted: dict[str, str] = {}
                    if source is not None and source.get("active", True):
                        old: dict[str, str] = {}
                        old_includes = (
                            existing.get("include_patches", []) if existing else []
                        )
                        if isinstance(old_includes, dict):
                            for old_repo, old_names in old_includes.items():
                                if (
                                    isinstance(old_repo, str)
                                    and old_repo.casefold() == key
                                    and isinstance(old_names, list)
                                ):
                                    old = {
                                        n.casefold(): n
                                        for n in old_names
                                        if isinstance(n, str)
                                    }
                        try:
                            records = get_compatible_patches(
                                canonical, payload.package_id
                            )
                        except PatchListError:
                            records = []
                        available: dict[str, str] = {}
                        for record in records:
                            name = record.get("name")
                            if isinstance(name, str):
                                folded = name.casefold()
                                if folded in available:
                                    raise ValueError
                                available[folded] = name
                        for name in names:
                            folded = name.casefold()
                            canonical_name = available.get(folded)
                            if canonical_name is None:
                                if folded not in old:
                                    raise ValueError
                                canonical_name = old[folded]
                            accepted[folded] = canonical_name
                    else:
                        accepted = {name.casefold(): name for name in names}
                    normalized[canonical] = list(accepted.values())
            except (ValueError, TypeError, AttributeError):
                raise HTTPException(400, detail="Invalid patch selection") from None
            newly_selected: dict[str, list[str]] = {}
            old_includes = existing.get("include_patches", []) if existing else []
            if isinstance(old_includes, dict):
                old_by_repo = {
                    repo.casefold(): {
                        name.casefold() for name in names if isinstance(name, str)
                    }
                    for repo, names in old_includes.items()
                    if isinstance(repo, str) and isinstance(names, list)
                }
                for repo, names in normalized.items():
                    source = canonical_sources.get(repo.casefold())
                    if source is None or not source.get("active", True):
                        continue
                    additions = [
                        name
                        for name in names
                        if name.casefold()
                        not in old_by_repo.get(repo.casefold(), set())
                    ]
                    if additions:
                        newly_selected[repo] = additions
            else:
                for repo, names in normalized.items():
                    source = canonical_sources.get(repo.casefold())
                    if source is not None and source.get("active", True):
                        newly_selected[repo] = names
            if newly_selected:
                try:
                    resolve_patch_selection(
                        {
                            "id": payload.package_id,
                            "name": payload.name,
                            "include_patches": newly_selected,
                        },
                        config,
                    )
                except (PatchListError, PatchSelectionError):
                    raise HTTPException(400, detail="Invalid patch selection") from None
            includes = normalized
        elif not all(isinstance(name, str) and name.strip() for name in includes):
            raise HTTPException(400, detail="Invalid patch selection")
        new_app = {
            "id": payload.package_id,
            "name": payload.name,
            "obtainium_id": f"morphe_{payload.package_id.split('.')[-1]}",
            "include_patches": includes,
            "exclude_patches": existing.get("exclude_patches", []) if existing else [],
        }
        # Preserve state (pinned version, last build, status) when editing an app
        if existing is not None:
            new_app = {**existing, **new_app}

        config["apps"] = [
            a for a in config.get("apps", []) if a["id"] != payload.package_id
        ]
        config.setdefault("apps", []).append(new_app)

    update_config(apply)
    return {"status": "success"}


class DeleteAppPayload(BaseModel):
    package_id: str


@app.post("/api/ui/delete_app")
def delete_app_from_config(payload: DeleteAppPayload):
    def apply(config: dict) -> None:
        config["apps"] = [
            a for a in config.get("apps", []) if a["id"] != payload.package_id
        ]

    update_config(apply)
    return {"status": "success"}


class AddSourcePayload(BaseModel):
    repo: str


def _claim_tools_sync() -> bool:
    """Claim the one background tool sync; False when one is already claimed.

    Returns:
        True when the caller now owns the claim and must end it by running
        `update_tools_task(..., claimed=True)`.
    """
    global TOOLS_UPDATING
    with _STATE_LOCK:
        if TOOLS_UPDATING:
            return False
        TOOLS_UPDATING = True
        return True


def update_tools_task(config: dict, force: bool = False, claimed: bool = False) -> None:
    """Sync the tool jars and active sources, one sync at a time.

    Args:
        config: The loaded config.
        force: Skip the update-check throttle.
        claimed: True when the caller already holds the claim from
            `_claim_tools_sync`. Otherwise one is taken here and, when another
            sync is running, nothing is started.
    """
    global TOOLS_UPDATING, SYNC_ERROR
    if not claimed and not _claim_tools_sync():
        logger.info("A tool sync is already running; not starting another")
        return
    SYNC_ERROR = None
    problems: list[str] = []
    try:
        # Sources that cannot be synced are skipped; say which, on the Discovery tab.
        problems.extend(plan_source_sync(config)[1])
        update_tools(config, quiet=True, force=force)
    except (GitHubRateLimitError, GitHubTokenRejectedError) as e:
        problems.append(str(e))
    finally:
        SYNC_ERROR = secrets_store.redact("; ".join(problems)) or None
        TOOLS_UPDATING = False


def _begin_activity(exclusive: bool = False) -> bool:
    """Record one more scraper activity (a refresh or a build).

    Args:
        exclusive: Refuse when a build is queued or running, or any activity
            is already going.

    Returns:
        True when the activity was recorded; the caller must call
        `_end_activity` once, in a `finally`.
    """
    global SCRAPER_ACTIVE, _ACTIVITY_COUNT
    with _STATE_LOCK:
        if exclusive and (SCRAPER_ACTIVE or BUILD_STATUS["in_progress"]):
            return False
        _ACTIVITY_COUNT += 1
        SCRAPER_ACTIVE = True
        return True


def _end_activity() -> None:
    """Record that one scraper activity ended; the flag drops with the last one."""
    global SCRAPER_ACTIVE, _ACTIVITY_COUNT
    with _STATE_LOCK:
        _ACTIVITY_COUNT = max(0, _ACTIVITY_COUNT - 1)
        SCRAPER_ACTIVE = _ACTIVITY_COUNT > 0


def refresh_versions_task() -> None:
    """Background task to refresh the version cache for all apps in the pipeline."""
    if not _begin_activity(exclusive=True):
        print("[Cron] Scraper busy, skipping refresh...")
        return

    def _do_refresh(scraper):
        config = load_config()
        print("[Cron] Refreshing remote version cache...")
        for app in config.get("apps", []):
            # Only refresh unpinned apps from APKMirror
            if not app.get("version"):
                time.sleep(2)
                try:
                    get_supported_versions(
                        app["id"], config, scraper=scraper, force_refresh=True
                    )
                except PatchListError as error:
                    logger.error("Version refresh for %s: %s", app["id"], error)

    try:
        SCRAPER_WORKER.execute(_do_refresh)
    except Exception as e:
        logger.error("Version refresh failed: %s", _redacted(e))
    finally:
        _end_activity()


def _collision_detail(other: str) -> str:
    """Error text for a new source whose local files an existing one already uses.

    Args:
        other: The existing source's repo, taken from the config (never from the
            request: a request value in an error text would be a masking oracle).

    Returns:
        The message.
    """
    return (
        "This source would use the same local files as the existing source "
        f"{other}. Remove {other} first."
    )


@app.post("/api/ui/add_source")
def add_source(payload: AddSourcePayload) -> dict[str, str]:
    """Sync a new patch source and add it to the config.

    The sync runs before the config is written, so a broken repository is never
    half-added. It holds the sync lock (not the config lock); when another sync
    is running the request is refused rather than queued.

    Args:
        payload: The repository, as `owner/repo` or a GitHub URL.

    Returns:
        `{"status": "success", "name": <source name>}`.

    Raises:
        HTTPException: 400 for an invalid, duplicate or colliding repository,
            409 while another sync runs, 429 on a GitHub rate limit, 502 when
            GitHub rejected the token or the repository lacks a patch list or
            bundle.
    """
    repo = normalize_repo(payload.repo)
    if not repo:
        raise HTTPException(
            400, "Invalid repository. Use 'owner/repo' or a GitHub URL."
        )

    config = load_config()
    sources = config.get("sources", [])
    if any(str(s.get("repo")).lower() == repo.lower() for s in sources):
        raise HTTPException(400, "This source already exists.")
    collides_with = find_file_collision(repo, sources)
    if collides_with is not None:
        raise HTTPException(400, _collision_detail(collides_with))

    # Sync before saving so a broken repo never ends up half-added, and the
    # patches .json exists before the frontend re-renders the app list.
    # Success is judged by the files on disk: the sync returns False both on
    # failure and when already up to date.
    # One sync at a time: when another one runs, this request is refused (409)
    # instead of waiting, so a request thread is never parked behind a long sync.
    # The sync lock is not the config lock; no config lock is held here.
    paths = get_source_paths(repo)
    if not SYNC_LOCK.acquire(blocking=False):
        raise HTTPException(
            409, "A sync is already running. Try again when it has finished."
        )
    try:
        remove_stale_download_files()
        check_and_download_release(repo, paths, force=True)
    except GitHubRateLimitError as e:
        raise HTTPException(429, str(e)) from e
    except GitHubTokenRejectedError as e:
        # 502, not 401: the browser's own request was fine, GitHub refused ours.
        raise HTTPException(502, str(e)) from e
    finally:
        SYNC_LOCK.release()
    if not paths[".json"].exists():
        raise HTTPException(
            502,
            "The repository is missing the patches JSON "
            "(patches-list.json or patches.json).",
        )
    if not paths[".mpp"].exists():
        raise HTTPException(
            502,
            "The repository has no .mpp patch bundle in its latest release "
            "(or no releases).",
        )

    name = f"{repo.split('/')[0]} Patches"
    new_source = {
        "name": name,
        "repo": repo,
        "active": True,
        "version": load_local_versions().get(repo),
    }

    def apply(current: dict) -> None:
        # Re-check against the file as it is now: the sync above was unlocked.
        current_sources = current.setdefault("sources", [])
        if any(str(s.get("repo")).lower() == repo.lower() for s in current_sources):
            raise HTTPException(400, "This source already exists.")
        other = find_file_collision(repo, current_sources)
        if other is not None:
            raise HTTPException(400, _collision_detail(other))
        current_sources.append(new_source)

    update_config(apply)
    return {"status": "success", "name": name}


@app.post("/api/ui/toggle_source")
def toggle_source(payload: AddSourcePayload):
    def apply(config: dict) -> None:
        for s in config.get("sources", []):
            if s["repo"] == payload.repo:
                s["active"] = not s.get("active", True)
                break

    update_config(apply)
    return {"status": "success"}


@app.post("/api/ui/delete_source")
def delete_source(payload: AddSourcePayload):
    def apply(config: dict) -> None:
        config["sources"] = [
            s for s in config.get("sources", []) if s["repo"] != payload.repo
        ]

    update_config(apply)
    return {"status": "success"}


# Android package id: two or more dot-separated Java identifiers.
PACKAGE_ID_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+")
# Version as it appears in a file name: no separators, at most 64 characters.
VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~-]{0,63}")


@app.post("/api/ui/upload_apk")
async def upload_apk(
    package_id: str = Form(""),
    version_str: str = Form(""),
    file: UploadFile = File(...),
):
    # Sanitize version string
    safe_version = version_str.strip().replace(" ", "_")
    if not PACKAGE_ID_PATTERN.fullmatch(package_id):
        raise HTTPException(status_code=400, detail="Invalid package id")
    if not VERSION_PATTERN.fullmatch(safe_version) or ".." in safe_version:
        raise HTTPException(status_code=400, detail="Invalid version string")

    raw_dir = DATA_DIR / "apks" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    file_path = raw_dir / f"{package_id}-{safe_version}.apk"
    config = load_config()
    app_config = next(
        (app for app in config.get("apps", []) if app.get("id") == package_id),
        {"id": package_id},
    )
    from src.patcher import (
        AndroidVariantInspectionError,
        AndroidVariantMetadataError,
        _read_minimum_sdk,
        extract_app_metadata,
        required_minimum_sdk,
    )

    minimum_sdk = required_minimum_sdk(app_config, config)
    staged_path = raw_dir / f".{package_id}-{safe_version}.{uuid.uuid4().hex}.tmp"
    actual_minimum_sdk = None
    try:
        staged_path.write_bytes(await file.read())
        if minimum_sdk is not None:
            try:
                actual_minimum_sdk = _read_minimum_sdk(
                    config.get("settings", {}).get("java_path", "java"), staged_path
                )
            except (
                AndroidVariantMetadataError,
                AndroidVariantInspectionError,
            ) as error:
                raise HTTPException(
                    status_code=400,
                    detail="Could not verify uploaded APK Android version",
                ) from error
            if actual_minimum_sdk < minimum_sdk:
                raise HTTPException(
                    status_code=400,
                    detail="Uploaded APK does not meet the required Android version",
                )

        current_config = load_config()
        current_app = next(
            (
                app
                for app in current_config.get("apps", [])
                if app.get("id") == package_id
            ),
            {"id": package_id},
        )
        current_minimum_sdk = required_minimum_sdk(current_app, current_config)
        if current_minimum_sdk is not None and actual_minimum_sdk is None:
            try:
                actual_minimum_sdk = _read_minimum_sdk(
                    current_config.get("settings", {}).get("java_path", "java"),
                    staged_path,
                )
            except (
                AndroidVariantMetadataError,
                AndroidVariantInspectionError,
            ) as error:
                raise HTTPException(
                    status_code=400,
                    detail="Could not verify uploaded APK Android version",
                ) from error
        if (
            current_minimum_sdk is not None
            and actual_minimum_sdk is not None
            and actual_minimum_sdk < current_minimum_sdk
        ):
            raise HTTPException(
                status_code=400,
                detail="Uploaded APK does not meet the required Android version",
            )

        def promote_if_still_eligible(current: dict) -> None:
            current_app = next(
                (app for app in current.get("apps", []) if app.get("id") == package_id),
                {"id": package_id},
            )
            required_now = required_minimum_sdk(current_app, current)
            if required_now is not None and (
                actual_minimum_sdk is None or actual_minimum_sdk < required_now
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Upload policy changed; retry with an eligible APK",
                )
            staged_path.replace(file_path)

        update_config(promote_if_still_eligible)
    finally:
        staged_path.unlink(missing_ok=True)

    extract_app_metadata(file_path, package_id)

    return {
        "status": "success",
        "message": f"Uploaded {file.filename} as {file_path.name}",
    }


@app.get("/api/ui/metadata")
def get_ui_metadata():
    """Returns the latest application metadata (names, versions, icons)."""
    metadata_file = DATA_DIR / "bin" / "metadata.json"
    if metadata_file.exists():
        with open(metadata_file, "r") as f:
            try:
                return json.load(f)
            except:
                return {}
    return {}


@app.get("/api/ui/discovery")
def refresh_discovery():
    """List the apps the patch sources support. Read only."""
    return {"apps": get_all_available_apps(load_config())}


@app.post("/api/ui/discovery/sync")
def sync_discovery(background_tasks: BackgroundTasks) -> dict:
    """Queue a forced tool and source sync unless one is already running.

    Args:
        background_tasks: FastAPI's task queue for work after the response.

    Returns:
        The current app list. When a sync is already queued or running, nothing
        is started and the answer also has `sync: "already running"`.
    """
    config = load_config()
    # Everything that can fail runs before the claim is taken: after it, only
    # queueing the task is left, so a failure cannot strand the claim.
    apps = get_all_available_apps(config)
    if SYNC_LOCK.locked() or not _claim_tools_sync():
        return {"apps": apps, "sync": "already running"}
    background_tasks.add_task(update_tools_task, config, True, True)
    return {"apps": apps}


SERVER_URL_INVALID = (
    "Server URL must look like http://host:port or https://host/prefix: http or "
    "https, a plain host name or address, an optional port and path, and no user "
    "name, query, fragment or spaces."
)


class SaveSettingsPayload(BaseModel):
    server_url: str
    cron_schedule: str
    notification_urls: object = None


NOTIFICATION_URLS_LOCK = threading.RLock()
APPRISE_URLS_INVALID = "Invalid notification URLs"
APPRISE_URLS_LOCKED = "Notification URLs are set by the environment"
APPRISE_MIGRATION_CONFLICT = "Notification URL migration conflict"
APPRISE_STORAGE_ERROR = "Could not store notification URLs"
APPRISE_REQUEST_TOO_LARGE = "Notification URL request is too large"
APPRISE_SETTINGS_REJECTED = "Notification URLs must be managed in Secrets"
APPRISE_MIGRATION_PATH = "/api/ui/secrets/apprise-urls/migration"
APPRISE_CREDENTIAL_FIELD = re.compile(
    r"(?:^|_)(?:password|passwd|token|secret|credential|auth|webhook|"
    r"api_key|access_key|private_key|client_secret)(?:$|_)",
    re.IGNORECASE,
)


def _legacy_apprise_urls(config: object) -> object:
    """Return the legacy config field as-is for comparison and migration."""
    if not isinstance(config, dict):
        return None
    settings = config.get("settings", {})
    return settings.get("notification_urls") if isinstance(settings, dict) else None


def _valid_legacy_urls(value: object) -> tuple[str, ...] | None:
    """Return a structurally valid legacy URL list, or None if malformed."""
    try:
        return secrets_store.validate_apprise_urls(value)
    except secrets_store.InvalidSecretError:
        return None


def _notification_secret_status(config: object) -> dict[str, object]:
    """Summarize URL ownership and migration state without returning values."""
    _effective_apprise_urls(config if isinstance(config, dict) else None)
    urls, source, valid = secrets_store.read_apprise_urls()
    legacy = _legacy_apprise_urls(config)
    legacy_available = isinstance(legacy, list) and bool(legacy)
    legacy_urls = _valid_legacy_urls(legacy)
    count = len(urls) if valid and urls is not None else 0
    if source is None:
        if legacy_available:
            source = "legacy"
            count = len(legacy) if isinstance(legacy, list) else 0
            migration_state = "pending"
        else:
            migration_state = "none"
    elif not legacy_available:
        migration_state = "none"
    elif source == "file" and valid and urls == legacy_urls:
        migration_state = "retry"
    else:
        migration_state = "conflict"
    return {
        "set": source in ("environment", "file", "legacy") and count > 0,
        "count": count,
        "source": source,
        "legacy_available": legacy_available,
        "migration_state": migration_state,
    }


def _effective_apprise_urls(config: dict | None = None) -> tuple[str, ...]:
    """Resolve new environment/file values before the legacy config fallback."""
    urls, source, valid = secrets_store.read_apprise_urls()
    if source is not None:
        effective = urls if valid and urls is not None else ()
    else:
        if config is None:
            config = load_config()
        legacy = _valid_legacy_urls(_legacy_apprise_urls(config))
        effective = legacy or ()
    if effective and _apprise_urls_are_supported(effective):
        _remember_apprise_urls(effective)
    return effective


def _apprise_redaction_values(urls: tuple[str, ...]) -> tuple[str, ...]:
    """Collect only URL strings and credential-named fields from Apprise parsing."""
    values: set[str] = set(urls)

    def private_fields(fields: object, schema: object) -> set[str]:
        if not isinstance(schema, str):
            return set()
        try:
            plugin = apprise_plugins.N_MGR[schema]
        except (KeyError, TypeError):
            return set()
        tokens = getattr(plugin, "template_tokens", {})
        if not isinstance(tokens, dict) or not isinstance(fields, dict):
            return set()
        result: set[str] = set()
        for token_name, metadata in tokens.items():
            if not isinstance(metadata, dict) or not metadata.get("private"):
                continue
            field_name = str(metadata.get("map_to") or token_name)
            value = fields.get(field_name)
            if isinstance(value, str) and value:
                result.add(value)
            elif isinstance(value, list):
                result.update(item for item in value if isinstance(item, str) and item)
        return result

    def collect(fields: object) -> None:
        if isinstance(fields, dict):
            for name, value in fields.items():
                field_name = str(name)
                normalized_name = re.sub(
                    r"(?<=[a-z0-9])(?=[A-Z])", "_", field_name
                ).replace("-", "_")
                if APPRISE_CREDENTIAL_FIELD.search(normalized_name) or (
                    normalized_name.lower() == "user"
                    and isinstance(value, str)
                    and len(value) >= 8
                ):
                    if isinstance(value, str) and value:
                        values.add(value)
                    elif isinstance(value, list):
                        values.update(
                            item for item in value if isinstance(item, str) and item
                        )
                collect(value)
        elif isinstance(fields, list):
            for item in fields:
                collect(item)

    for url in urls:
        fields = apprise_plugins.url_to_dict(url, secure_logging=True)
        if fields is not None:
            values.update(private_fields(fields, fields.get("schema")))
            collect(fields)
    return tuple(sorted(values, key=len, reverse=True))


def _remember_apprise_urls(urls: tuple[str, ...]) -> None:
    """Publish masks from accepted URLs to all process logging threads."""
    with secrets_store.temporary_redactions(urls):
        values = _apprise_redaction_values(urls)
        secrets_store.remember_apprise_masks(values)


def _has_decoded_newline(url: str) -> bool:
    """Reject literal or URL-encoded line separators before Apprise parsing."""
    decoded = url
    for _ in range(8):
        if secrets_store.contains_line_separator(decoded):
            return True
        next_value = unquote(decoded)
        if next_value == decoded:
            return False
        decoded = next_value
    return secrets_store.contains_line_separator(decoded)


def _apprise_urls_are_supported(urls: tuple[str, ...]) -> bool:
    """Check URL syntax with Apprise locally, without sending notifications."""
    if not urls or any(_has_decoded_newline(url) for url in urls):
        return False
    with secrets_store.temporary_redactions(urls):
        try:
            redactions = _apprise_redaction_values(urls)
            with secrets_store.temporary_redactions(redactions):
                apobj = apprise.Apprise()
                for url in urls:
                    before = len(apobj.servers)
                    if not apobj.add(url) or len(apobj.servers) - before != 1:
                        return False
                return True
        except Exception:  # noqa: BLE001 - parser failure has a fixed API error
            return False


def _send_apprise(urls: tuple[str, ...], title: str, message: str) -> bool:
    """Validate and deliver under a temporary, per-thread URL redaction scope."""
    if not _apprise_urls_are_supported(urls):
        return False
    redactions = _apprise_redaction_values(urls)
    try:
        with (
            secrets_store.active_apprise_masks((*urls, *redactions)),
            secrets_store.temporary_redactions((*urls, *redactions)),
        ):
            try:
                apobj = apprise.Apprise()
                for url in urls:
                    before = len(apobj.servers)
                    if not apobj.add(url) or len(apobj.servers) - before != 1:
                        return False
                secrets_store.remember_apprise_masks(redactions)
                return bool(
                    apobj.notify(
                        body=secrets_store.redact(message),
                        title=secrets_store.redact(title),
                    )
                )
            except Exception as error:  # noqa: BLE001 - fixed, redacted failure
                logger.error("Apprise notification failed: %s", _redacted(error))
                return False
    except secrets_store.AppriseRedactionCapacityError:
        logger.error("Could not start notification because redaction capacity is full")
        return False


async def _read_apprise_urls_value(request: Request) -> tuple[str, ...]:
    """Read a bounded URL-list payload and return fixed errors without echoing it."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > secrets_store.MAX_APPRISE_STORAGE_BYTES:
        raise HTTPException(status_code=413, detail=APPRISE_REQUEST_TOO_LARGE)
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > secrets_store.MAX_APPRISE_STORAGE_BYTES:
            raise HTTPException(status_code=413, detail=APPRISE_REQUEST_TOO_LARGE)
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks))
        value = body.get("value") if isinstance(body, dict) else None
        urls = secrets_store.validate_apprise_urls(value)
    except (ValueError, RecursionError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail=APPRISE_URLS_INVALID) from None
    if not _apprise_urls_are_supported(urls):
        raise HTTPException(status_code=400, detail=APPRISE_URLS_INVALID)
    return urls


def _remove_legacy_apprise_urls(expected: object) -> None:
    """Remove legacy URLs only if they still match the observed config value."""

    def apply(config: dict) -> None:
        settings = config.setdefault("settings", {})
        current = settings.get("notification_urls")
        if current != expected:
            raise ValueError("Legacy notification URLs changed")
        settings.pop("notification_urls", None)

    update_config(apply)


def send_notification(title: str, message: str, config=None):
    """Send a notification to the configured URLs; secret values are removed."""
    if not config:
        config = load_config()

    urls = _effective_apprise_urls(config)
    if not urls:
        return
    _send_apprise(urls, secrets_store.redact(title), secrets_store.redact(message))


@app.post("/api/ui/test_notification")
def test_notification():
    urls = _effective_apprise_urls()
    if not urls:
        return {"status": "error", "message": "No notification URLs provided"}
    if not _apprise_urls_are_supported(urls):
        raise HTTPException(status_code=400, detail=APPRISE_URLS_INVALID)
    success = _send_apprise(
        urls,
        "Morphe Builder Test",
        "This is a test notification from Morphe Builder.",
    )
    return {"status": "success" if success else "error"}


CRON_LOCK = threading.Lock()


@app.post("/api/ui/save_settings")
def save_settings(payload: SaveSettingsPayload) -> dict[str, str]:
    """Save the server URL, cron schedule and notification URLs.

    Args:
        payload: The new settings.

    Returns:
        `{"status": "success"}`.

    Raises:
        HTTPException: 400 with a fixed message when the server URL is not
            `http(s)://host[:port][/prefix]`, is not empty and differs from
            the stored one; nothing is written then.
    """
    if payload.notification_urls not in (None, []):
        raise HTTPException(status_code=400, detail=APPRISE_SETTINGS_REJECTED)
    with NOTIFICATION_URLS_LOCK:
        stored = load_config().get("settings", {}).get("server_url")
        server_url_from_environment = "SERVER_URL" in os.environ
        cron_from_environment = "CRON_SCHEDULE" in os.environ
        unchanged = isinstance(stored, str) and payload.server_url.rstrip(
            "/"
        ) == stored.rstrip("/")
        # A value already stored never blocks saving the other settings (the page
        # posts all of them); only a changed value has to be valid. Empty means
        # unset.
        acceptable = (
            server_url_from_environment or unchanged or payload.server_url == ""
        )
        if not acceptable and not request_guards.is_valid_server_url(
            payload.server_url
        ):
            raise HTTPException(status_code=400, detail=SERVER_URL_INVALID)

        def apply(config: dict) -> None:
            settings = config.setdefault("settings", {})
            if not server_url_from_environment:
                settings["server_url"] = payload.server_url.rstrip("/")
            if not cron_from_environment:
                settings["cron_schedule"] = payload.cron_schedule

        update_config(apply)
        with CRON_LOCK:
            setup_cron_job(load_config())
    return {"status": "success"}


SECRET_SLUGS = {
    "github-token": secrets_store.GITHUB_TOKEN,
}
MAX_SECRET_BODY_BYTES = 4096


def _secret_status() -> dict[str, dict[str, object]]:
    """Return `{slug: {"set": bool, "source": str | None}}`, never a value.

    `source` is "environment_invalid" (and `set` False) for a variable that is
    set but not a valid secret, so the operator can tell it is being ignored.
    """
    try:
        sources = secrets_store.get_status()
    except OSError:
        logger.error("Could not read the secrets file")
        raise HTTPException(
            status_code=500, detail="Could not read the secrets file"
        ) from None
    result: dict[str, dict[str, object]] = {
        slug: {
            "set": sources[name] in ("environment", "file"),
            "source": sources[name],
        }
        for slug, name in SECRET_SLUGS.items()
    }
    try:
        with NOTIFICATION_URLS_LOCK:
            try:
                config = load_config()
            except yaml.YAMLError:
                logger.warning("Could not parse config.yaml for notification status")
                config = {}
            result["apprise-urls"] = _notification_secret_status(config)
    except OSError:
        logger.error("Could not read notification URL storage")
        raise HTTPException(status_code=500, detail=APPRISE_STORAGE_ERROR) from None
    return result


def _secret_name(slug: str) -> str:
    """Map a route slug to a secret name, or raise a 404."""
    name = SECRET_SLUGS.get(slug)
    if name is None:
        raise HTTPException(status_code=404, detail="Unknown secret")
    return name


async def _read_secret_value(request: Request) -> str:
    """Read `{"value": "..."}` by hand so no error body can echo the input.

    Raises:
        HTTPException: 400 with a fixed message for any malformed or invalid body.
    """
    invalid = HTTPException(status_code=400, detail="Invalid secret value")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_SECRET_BODY_BYTES:
        raise invalid
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_SECRET_BODY_BYTES:
            raise invalid
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks))
    except (ValueError, RecursionError):
        raise invalid from None
    value = body.get("value") if isinstance(body, dict) else None
    try:
        return secrets_store.validate_secret(value)
    except secrets_store.InvalidSecretError:
        raise invalid from None


def _change_secret(name: str, value: str | None) -> dict[str, dict[str, object]]:
    """Set (value given) or clear a secret and return the new status."""
    try:
        if value is None:
            secrets_store.clear_secret(name)
        else:
            secrets_store.set_secret(name, value)
    except secrets_store.SecretFromEnvironmentError as error:
        detail = "Secret is set by the environment"
        if error.invalid:
            detail += " but its value is invalid"
        raise HTTPException(status_code=409, detail=detail) from None
    except OSError:
        logger.error("Could not write the secrets file for %s", name)
        raise HTTPException(
            status_code=500, detail="Could not write the secrets file"
        ) from None
    return _secret_status()


@app.get("/api/ui/secrets")
def get_secrets():
    """Report whether each secret is set and where it comes from."""
    return _secret_status()


def _store_apprise_urls(urls: tuple[str, ...]) -> dict[str, dict[str, object]]:
    """Persist URLs first, then remove matching legacy values if present."""
    with NOTIFICATION_URLS_LOCK:
        config = load_config()
        legacy = _legacy_apprise_urls(config)
        try:
            secrets_store.set_apprise_urls(list(urls))
            _remember_apprise_urls(urls)
        except secrets_store.InvalidAppriseFileError:
            raise HTTPException(
                status_code=409, detail=APPRISE_MIGRATION_CONFLICT
            ) from None
        except secrets_store.SecretFromEnvironmentError:
            raise HTTPException(status_code=409, detail=APPRISE_URLS_LOCKED) from None
        except OSError:
            logger.error("Could not write notification URL storage")
            raise HTTPException(status_code=500, detail=APPRISE_STORAGE_ERROR) from None
        if isinstance(legacy, list):
            try:
                _remove_legacy_apprise_urls(legacy)
            except (OSError, ValueError):
                logger.error("Could not remove legacy notification URLs")
                raise HTTPException(
                    status_code=500, detail=APPRISE_STORAGE_ERROR
                ) from None
        return _secret_status()


@app.put("/api/ui/secrets/apprise-urls")
async def put_apprise_urls(request: Request) -> dict[str, dict[str, object]]:
    """Replace the write-only notification URL list."""
    urls = await _read_apprise_urls_value(request)
    return _store_apprise_urls(urls)


@app.delete("/api/ui/secrets/apprise-urls")
def delete_apprise_urls() -> dict[str, dict[str, object]]:
    """Clear notification URLs and remove any legacy fallback values."""
    with NOTIFICATION_URLS_LOCK:
        config = load_config()
        legacy = _legacy_apprise_urls(config)
        try:
            # An empty owned value is a tombstone: it suppresses legacy fallback
            # even if config cleanup cannot complete.
            secrets_store.set_apprise_urls([])
            _remember_apprise_urls(())
        except secrets_store.InvalidAppriseFileError:
            raise HTTPException(
                status_code=409, detail=APPRISE_MIGRATION_CONFLICT
            ) from None
        except secrets_store.SecretFromEnvironmentError:
            raise HTTPException(status_code=409, detail=APPRISE_URLS_LOCKED) from None
        except OSError:
            logger.error("Could not clear notification URL storage")
            raise HTTPException(status_code=500, detail=APPRISE_STORAGE_ERROR) from None
        if isinstance(legacy, list):
            try:
                _remove_legacy_apprise_urls(legacy)
            except (OSError, ValueError):
                logger.error("Could not remove legacy notification URLs")
                raise HTTPException(
                    status_code=500, detail=APPRISE_STORAGE_ERROR
                ) from None
        return _secret_status()


@app.post(APPRISE_MIGRATION_PATH)
def migrate_apprise_urls() -> dict[str, dict[str, object]]:
    """Explicitly move the legacy notification list into write-only storage."""
    with NOTIFICATION_URLS_LOCK:
        try:
            urls, source, valid = secrets_store.read_apprise_urls()
            config = load_config()
            legacy = _legacy_apprise_urls(config)
            if source in ("environment", "environment_invalid"):
                raise HTTPException(status_code=409, detail=APPRISE_MIGRATION_CONFLICT)
            if source == "file_invalid":
                raise HTTPException(status_code=409, detail=APPRISE_MIGRATION_CONFLICT)
            if not isinstance(legacy, list):
                return _secret_status()
            legacy_urls = _valid_legacy_urls(legacy)
            if legacy_urls is None or not _apprise_urls_are_supported(legacy_urls):
                raise HTTPException(status_code=409, detail=APPRISE_MIGRATION_CONFLICT)
            if source == "file":
                if not valid or urls != legacy_urls:
                    raise HTTPException(
                        status_code=409, detail=APPRISE_MIGRATION_CONFLICT
                    )
                _remember_apprise_urls(legacy_urls)
            else:
                try:
                    secrets_store.set_apprise_urls(list(legacy_urls))
                    _remember_apprise_urls(legacy_urls)
                except secrets_store.InvalidAppriseFileError:
                    raise HTTPException(
                        status_code=409, detail=APPRISE_MIGRATION_CONFLICT
                    ) from None
                except secrets_store.SecretFromEnvironmentError:
                    raise HTTPException(
                        status_code=409, detail=APPRISE_MIGRATION_CONFLICT
                    ) from None
            try:
                _remove_legacy_apprise_urls(legacy)
            except (OSError, ValueError):
                logger.error(
                    "Could not remove legacy notification URLs after migration"
                )
                raise HTTPException(
                    status_code=500, detail=APPRISE_STORAGE_ERROR
                ) from None
            return _secret_status()
        except HTTPException:
            raise
        except OSError:
            logger.error("Could not migrate notification URL storage")
            raise HTTPException(status_code=500, detail=APPRISE_STORAGE_ERROR) from None


def _schedule_tools_sync(config: dict) -> None:
    """Hand a forced tool sync, whose claim is already taken, to the scheduler.

    A scheduler job, not a Starlette background task: a background task runs
    only if the response was fully sent, and a task that never runs would leave
    the claim held for good. No misfire limit, so a job that starts late still
    runs and releases the claim.

    Args:
        config: The loaded config the sync works from.
    """
    scheduler.add_job(
        update_tools_task,
        id=f"token_sync_{uuid.uuid4().hex}",
        args=[config, True, True],
        misfire_grace_time=None,
    )


def _sync_after_token_change(name: str, changed: bool) -> str | None:
    """Schedule one forced tool sync after the GitHub token changed.

    The sync shows what the new token can do (`github_token_permissions`,
    `github_token_rejected`). It shares the one-at-a-time claim of the
    Discovery sync. Blocking (reads config.yaml); call it off the event loop.

    Args:
        name: The secret that was just written.
        changed: Whether the stored value differs from before the write.

    Returns:
        None for any other secret. Otherwise the `X-Sync` value: "queued",
        "already-running" (the running sync may have started with the old
        token; nothing is queued), "not-needed" (the token did not change) or
        "unavailable" (config.yaml unusable or the scheduler refused the job).
    """
    if name != secrets_store.GITHUB_TOKEN:
        return None
    if not changed:
        return "not-needed"
    # Everything that can fail runs before the claim is taken (see
    # `sync_discovery`); a failure here must not undo the saved token.
    try:
        config = load_config()
    except (OSError, yaml.YAMLError):
        logger.error("Could not read config.yaml; no sync queued after token change")
        return "unavailable"
    if not isinstance(config, dict):
        logger.error("config.yaml is not a mapping; no sync queued after token change")
        return "unavailable"
    if SYNC_LOCK.locked() or not _claim_tools_sync():
        return "already-running"
    scheduled = False
    try:
        _schedule_tools_sync(config)
        scheduled = True
    except Exception:  # the saved token must stand; the claim is released below
        logger.exception("Could not schedule the sync after the token change")
    finally:
        if not scheduled:
            _release_tools_sync()
    return "queued" if scheduled else "unavailable"


def _release_tools_sync() -> None:
    """Give back a claim from `_claim_tools_sync` that no task will end."""
    global TOOLS_UPDATING
    with _STATE_LOCK:
        TOOLS_UPDATING = False


def _change_secret_and_sync(name: str, value: str | None) -> tuple[dict, str | None]:
    """Write or clear a secret, then schedule the token sync if one is due.

    Returns:
        The new status and the `X-Sync` value (see `_sync_after_token_change`).

    Raises:
        HTTPException: As `_change_secret`; nothing is scheduled then.
    """
    try:
        before = secrets_store.get_secret(name)
    except OSError:
        before = None  # the write below reports an unreadable file
    status = _change_secret(name, value)
    return status, _sync_after_token_change(name, before != value)


def _with_sync_header(result: tuple[dict, str | None], response: Response) -> dict:
    """Put the `X-Sync` header on `response` and return the status body."""
    status, sync = result
    if sync is not None:
        response.headers["X-Sync"] = sync
    return status


@app.put("/api/ui/secrets/{slug}")
async def put_secret(slug: str, request: Request, response: Response):
    """Store a secret from `{"value": "..."}`; 409 if the environment sets it.

    Changing the GitHub token also schedules one background tool sync and says
    so in the `X-Sync` header: "queued", "already-running", "not-needed" or
    "unavailable". The body is the secret status.
    """
    name = _secret_name(slug)
    value = await _read_secret_value(request)
    result = await run_in_threadpool(_change_secret_and_sync, name, value)
    return _with_sync_header(result, response)


@app.delete("/api/ui/secrets/{slug}")
def delete_secret(slug: str, response: Response):
    """Remove a stored secret; 409 if the environment sets it.

    Clearing a stored GitHub token also schedules a sync (`X-Sync`, as for PUT).
    """
    result = _change_secret_and_sync(_secret_name(slug), None)
    return _with_sync_header(result, response)


class Tee(io.TextIOBase):
    def __init__(self, *files):
        self.files = files

    def write(self, obj):
        text = secrets_store.redact(obj)
        for f in self.files:
            f.write(text)
            f.flush()
        return len(obj)


class TriggerBuildPayload(BaseModel):
    package_ids: List[str] | None = None


def _claim_build() -> bool:
    """Claim the one build slot, at request time.

    Returns:
        True when the caller now owns the slot: `in_progress` is raised and
        stays raised until the owner's `build_task` ends. False when a build is
        already queued or running.
    """
    with _STATE_LOCK:
        if BUILD_STATUS["in_progress"]:
            return False
        BUILD_STATUS["in_progress"] = True
        BUILD_STATUS["message"] = "Queued"
        return True


def _release_build() -> None:
    """Give the build slot back; only the build that claimed it calls this."""
    with _STATE_LOCK:
        BUILD_STATUS["in_progress"] = False


def build_task(
    package_ids: list[str] | None = None,
    is_cron: bool = False,
    claimed: bool = False,
) -> None:
    """Run one build on the scraper worker and always give the slot back.

    At most one build is queued or running: the slot is claimed atomically when
    the request is accepted and released only here, by the build that owns it.

    Args:
        package_ids: Apps to build; None or empty means all.
        is_cron: True for the scheduled build (quiet, once per minute).
        claimed: True when the caller (`trigger_build`) already claimed the
            slot. Otherwise it is claimed here and the build is skipped when
            another one is queued or running.
    """
    current_time_str = time.strftime("%Y-%m-%d %H:%M")
    if not claimed:
        if is_cron and BUILD_STATUS.get("last_run_minute") == current_time_str:
            return
        if not _claim_build():
            logger.warning(
                "A %s build was not started: another build is queued or running",
                "scheduled" if is_cron else "manual",
            )
            return

    def _do_build(scraper):
        BUILD_STATUS["message"] = "Starting..."
        if is_cron:
            BUILD_STATUS["last_run_minute"] = current_time_str

        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "w", buffering=1, encoding="utf-8", newline="") as log_file:
            old_stdout = sys.stdout
            sys.stdout = Tee(sys.stdout, log_file)
            try:
                from src.patcher import run_pipeline

                # Pass the persistent scraper to the pipeline
                patched_ids = run_pipeline(package_ids, quiet=is_cron, scraper=scraper)
                BUILD_STATUS["message"] = "Finished"
                BUILD_STATUS["last_run"] = time.strftime("%H:%M:%S")

                config = load_config()
                all_apps = {a["id"]: a["name"] for a in config.get("apps", [])}

                if patched_ids:
                    patched_names = [all_apps.get(pid, pid) for pid in patched_ids]
                    send_notification(
                        "Apps Patched Successfully",
                        f"Updated: {', '.join(patched_names)}",
                    )
                elif not is_cron:
                    send_notification("Build Finished", "No updates or build failed.")
            except Exception as e:
                message = secrets_store.redact(str(e))
                # A print, not a log call: this stdout is the build log the UI shows.
                print(f"\n[FATAL ERROR] {message}")
                BUILD_STATUS["message"] = f"Failed: {message}"
                send_notification("Build Failed", f"Pipeline error: {message}")
            finally:
                sys.stdout = old_stdout
                # Invalidate update cache when build finishes
                _invalidate_updates()

    _begin_activity()
    try:
        SCRAPER_WORKER.execute(_do_build)
    except Exception as e:
        # Raised outside the build body (browser down): nothing else reports it.
        message = _redacted(e)
        logger.error("Build task failed: %s", message)
        BUILD_STATUS["message"] = f"Failed: {message}"
    finally:
        _end_activity()
        _release_build()


@app.post("/api/ui/trigger_build")
def trigger_build(payload: TriggerBuildPayload) -> dict[str, str]:
    """Accept a manual build unless one is already queued or running.

    Args:
        payload: Optional list of package ids; None builds all.

    Returns:
        `{"status": "Build started"}`, or `{"status": "Build already in
        progress"}` when the build slot is taken (no second build is queued).
    """
    if not _claim_build():
        return {"status": "Build already in progress"}

    scheduled = False
    try:
        # Invalidate cache to ensure UI shows correct status after trigger
        _invalidate_updates()

        # Use an empty list if None is sent to signify "Build All" but manual
        pkg_ids = payload.package_ids if payload.package_ids is not None else []

        # Use scheduler instead of BackgroundTasks to ensure it runs in a clean
        # thread for Playwright compatibility. A random id: two triggers in one
        # second must not collide. No misfire grace limit: a job that starts late
        # (all pool threads busy, a stalled clock) must still run, or the claim
        # taken above would never be released.
        scheduler.add_job(
            build_task,
            id=f"manual_build_{uuid.uuid4().hex}",
            kwargs={"package_ids": pkg_ids, "is_cron": False, "claimed": True},
            misfire_grace_time=None,
        )
        scheduled = True
    finally:
        if not scheduled:
            _release_build()

    return {"status": "Build started"}


@app.get("/api/ui/build_status")
def get_build_status():
    """Return the build state; the message has secret values removed."""
    return {**BUILD_STATUS, "message": _redacted_text(BUILD_STATUS.get("message"))}


@app.get("/api/ui/tools_status")
def get_tools_status() -> dict:
    """Report the sync, diagnostics and browser state shown in the header.

    Returns:
        `updating`, `sync_error`, `github_token_warning`,
        `github_token_rejected`, `github_token_permissions` (null, "unchecked",
        "none", "present" or "unverifiable"), `diagnostics`, `browser` (the
        worker status, including `busy_since`), `server_time` (epoch seconds,
        the clock `busy_since` is measured on) and `data_dir`.
    """
    return {
        "updating": TOOLS_UPDATING,
        "sync_error": _redacted_text(SYNC_ERROR),
        # Not redacted: built from validated scope names and fixed text, so
        # masking could only remove a scope that happens to match a secret.
        "github_token_warning": github_token_warning(),
        "github_token_rejected": github_token_rejected(),
        "github_token_permissions": github_token_permissions(),
        "server_time": time.time(),
        "diagnostics": DIAGNOSTICS,
        "browser": SCRAPER_WORKER.status(),
        "data_dir": str(DATA_DIR),
    }


@app.get("/api/ui/logs")
def get_logs():
    """Return the build log, with secret values removed."""
    if not LOG_PATH.exists():
        return {"logs": ""}
    # Use newline="" to prevent \r being converted to \n
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace", newline="") as f:
        return {"logs": secrets_store.redact(f.read())}


# --- OBTAINIUM ROUTES ---


@app.get("/download/{package_name}")
def download_app_by_name(package_name: str):
    """Serves the latest patched APK for a given package name or exact filename."""

    # Priority 1: Exact filename match in patched directory
    file_path = DATA_DIR / "apks" / "patched" / package_name
    if file_path.exists() and file_path.is_file():
        return FileResponse(
            file_path, media_type="application/vnd.android.package-archive"
        )

    # Clean package name to handle .apk extension if present for globbing
    base_name = package_name.replace("-patched.apk", "").replace(".apk", "")
    # If package_name contains a version (has a dash), split it to get the ID
    if "-" in base_name:
        base_name = base_name.split("-")[0]

    # Priority 2: newest file of exactly this package. File names are
    # <pkg>-<rest>.apk (or <pkg>.apk) and package ids never contain "-".
    patched_dir = DATA_DIR / "apks" / "patched"
    if patched_dir.is_dir():
        matches = [
            f
            for f in patched_dir.iterdir()
            if f.is_file()
            and f.suffix == ".apk"
            and (f.name == f"{base_name}.apk" or f.name.startswith(f"{base_name}-"))
        ]
        if matches:
            newest = max(matches, key=lambda f: f.stat().st_mtime)
            return FileResponse(
                newest, media_type="application/vnd.android.package-archive"
            )

    # Fixed text: an error built from the request would let the secret masking
    # confirm whether the requested name held a secret.
    raise HTTPException(status_code=404, detail="No patched APK found")


@app.get("/view/{package_id}", response_class=HTMLResponse)
async def view_app(package_id: str, request: Request):
    """Serves a minimal HTML page for Obtainium to parse."""
    config = load_config()
    # Try to find by package ID first, then fallback to obtainium_id
    app_info = next((a for a in config.get("apps", []) if a["id"] == package_id), None)
    if not app_info:
        app_info = next(
            (a for a in config.get("apps", []) if a.get("obtainium_id") == package_id),
            None,
        )

    if not app_info:
        return HTMLResponse("App not found in pipeline", status_code=404)

    last_build = app_info.get("last_successful_build", {})
    current_version = last_build.get("combined_version") or last_build.get(
        "app_version", "Unknown"
    )

    # Resolve the correct filename:
    # 1. Check if metadata has it
    # 2. Check if the file actually exists
    # 3. Fallback to globbing the latest on disk
    filename = last_build.get("filename")
    patched_dir = DATA_DIR / "apks" / "patched"
    if not filename or not (patched_dir / filename).exists():
        matches = list(patched_dir.glob(f"{app_info['id']}-*.apk"))
        if matches:
            matches.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            filename = matches[0].name
        else:
            filename = f"{app_info['id']}-patched.apk"  # Legacy fallback

    patch_versions = last_build.get("patch_versions", {})

    # Just show first few patch versions if many exist
    patch_version_str = ", ".join(
        [f"{repo.split('/')[-1]}:{v}" for repo, v in patch_versions.items()]
    )

    # Build absolute URL for the download link to help some parsers
    settings = config.get("settings", {})
    base_url = settings.get("server_url", str(request.base_url).rstrip("/"))
    download_url = f"{base_url}/download/{filename}"

    return templates.TemplateResponse(
        request=request,
        name="obtainium.html",
        context={
            "app": app_info,
            "version": current_version,
            "patch_version": patch_version_str,
            "download_url": download_url,
        },
    )


@app.get("/api/apps/{obtainium_id}")
def get_single_app(obtainium_id: str, request: Request):
    """Provides Obtainium metadata for a specific application."""
    config = load_config()
    settings = config.get("settings", {})
    base_url = settings.get("server_url", str(request.base_url).rstrip("/"))

    app_info = next(
        (a for a in config.get("apps", []) if a.get("obtainium_id") == obtainium_id),
        None,
    )
    if not app_info:
        raise HTTPException(status_code=404, detail="App not found")

    last_build = app_info.get("last_successful_build", {})
    version = last_build.get("combined_version") or last_build.get(
        "app_version", "Unknown"
    )

    # Resolve filename with fallback
    filename = last_build.get("filename")
    patched_dir = DATA_DIR / "apks" / "patched"
    if not filename or not (patched_dir / filename).exists():
        matches = list(patched_dir.glob(f"{app_info['id']}-*.apk"))
        if matches:
            matches.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            filename = matches[0].name
        else:
            filename = f"{app_info['id']}-patched.apk"

    # CRITICAL: Obtainium needs the 'id' to match the actual Android Package Name
    # to avoid the "could not get ID from apk" error.
    response = {
        "id": app_info["id"],
        "name": app_info["name"],
        "version": version,
        "download_url": f"{base_url}/download/{filename}",
    }
    return response


@app.get("/api/apps")
def get_apps(request: Request):
    config = load_config()
    settings = config.get("settings", {})
    base_url = settings.get("server_url", str(request.base_url).rstrip("/"))

    response_data = []
    patched_dir = DATA_DIR / "apks" / "patched"
    for app_info in config.get("apps", []):
        last_build = app_info.get("last_successful_build", {})
        version = last_build.get("combined_version") or last_build.get(
            "app_version", "Unknown"
        )

        # Resolve filename with fallback
        filename = last_build.get("filename")
        if not filename or not (patched_dir / filename).exists():
            matches = list(patched_dir.glob(f"{app_info['id']}-*.apk"))
            if matches:
                matches.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                filename = matches[0].name
            else:
                filename = f"{app_info['id']}-patched.apk"

        response_data.append(
            {
                "id": app_info["id"],  # Match real package name
                "name": app_info["name"],
                "version": version,
                "download_url": f"{base_url}/download/{filename}",
            }
        )
    return {"apps": response_data}


# The only scheduler jobs `setup_cron_job` owns. Others (a pending manual build,
# the initial version refresh) are never touched by it.
CRON_JOB_IDS = ("periodic_build", "version_refresh")


def validate_environment_settings() -> None:
    """Reject invalid environment overrides before startup proceeds."""
    if "SERVER_URL" in os.environ and not request_guards.is_valid_server_url(
        os.environ["SERVER_URL"]
    ):
        raise ValueError("Invalid SERVER_URL: expected a non-empty http(s) URL")
    if cron_schedule := os.environ.get("CRON_SCHEDULE"):
        try:
            CronTrigger.from_crontab(cron_schedule)
        except ValueError as error:
            raise ValueError(
                "Invalid CRON_SCHEDULE: expected a five-field crontab expression"
            ) from error


def setup_cron_job(config: dict) -> None:
    """Replace the scheduled build and version refresh from the settings.

    Args:
        config: The loaded config; `settings.cron_schedule` may be empty.
    """
    cron = config.get("settings", {}).get("cron_schedule")
    for job_id in CRON_JOB_IDS:
        try:
            scheduler.remove_job(job_id)
        except JobLookupError:
            pass  # not scheduled, or already gone: nothing to remove

    if cron:
        try:
            # For cron, we pass package_ids=None (default) and is_cron=True
            scheduler.add_job(
                build_task,
                CronTrigger.from_crontab(cron),
                id="periodic_build",
                kwargs={"is_cron": True},
            )
            print(f"[Cron] Scheduled background build with: {cron}")

            # Refresh versions every hour to keep UI up to date
            scheduler.add_job(
                refresh_versions_task, CronTrigger(hour="*"), id="version_refresh"
            )
            print("[Cron] Scheduled hourly version cache refresh")
        except Exception as e:  # noqa: BLE001 - scheduling must not stop the app
            logger.error("Cron scheduling failed: %s", _redacted(e))


@app.on_event("startup")
async def startup_event() -> None:
    """Start the worker and scheduler, and sync the tools if they are missing."""
    global DIAGNOSTICS
    validate_environment_settings()
    # First, before anything is started: take the secrets out of os.environ so
    # no thread, subprocess or library inherits them.
    secrets_store.absorb_environment()
    ensure_data_dirs()
    remove_legacy_cloakbrowser_cache()
    config = load_config()
    _effective_apprise_urls(config)
    setup_cron_job(config)
    DIAGNOSTICS = check_dependencies(config)

    # Start the persistent scraper worker
    SCRAPER_WORKER.start()

    cli_jar = DATA_DIR / config["tools"]["cli_jar"]
    apkeditor_jar = DATA_DIR / config["tools"]["apkeditor_jar"]
    if not cli_jar.exists() or not apkeditor_jar.exists():
        print("[Init] Tools missing, triggering initial sync...")
        update_tools_task(config)

    scheduler.start()
    # Trigger an immediate refresh of versions in the background
    scheduler.add_job(refresh_versions_task, id="initial_version_refresh")


@app.on_event("shutdown")
def shutdown_event():
    """Stop the worker and the scheduler; never raises.

    The worker is told first, so a scheduler problem cannot skip it. A running
    build is not cancelled: the scheduler is not waited for, and the worker
    finishes the build before it closes the browser.

    This does not make the process exit early. Only the worker thread is a
    daemon. The scheduler's job thread that runs `build_task` stays blocked in
    `ScraperWorker.execute` until the worker answers, and the interpreter waits
    for that thread at exit, so the process ends when the running build ends
    (`SHUTDOWN_JOIN_SECONDS` only bounds how long this handler waits).
    """
    print("[Shutdown] Stopping scheduler...")
    SCRAPER_WORKER.shutdown()
    try:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    except Exception as error:  # noqa: BLE001 - shutdown must still stop the worker
        logger.error("Scheduler shutdown failed: %s", error)
    if SCRAPER_WORKER.is_alive():
        SCRAPER_WORKER.join(timeout=SHUTDOWN_JOIN_SECONDS)
