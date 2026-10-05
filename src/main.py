from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
import json
import math
import re
import sys
import io
import time
import apprise
from playwright.sync_api import Error as PlaywrightError
from dataclasses import dataclass
from typing import Any, Callable, List, Protocol, TypeVar, cast
from pathlib import Path
from src.patcher import (
    get_supported_versions,
    load_patch_list,
    PatchListError,
    VersionLookupError,
    get_all_available_apps,
    get_source_paths,
    normalize_repo,
    update_tools,
    get_current_version,
    check_dependencies,
    check_and_download_release,
    load_local_versions,
    selected_patches,
    is_base_repo,
    DATA_DIR,
    GitHubRateLimitError,
)
from packaging import version
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import threading
import asyncio
import logging
import queue
from src import secrets_store
from src.request_guards import RequestGuardMiddleware
from src.config_store import load_config, update_config

logger = logging.getLogger(__name__)
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
        launch: Callable[[], Downloader] = _launch_downloader,
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
        try:
            self.downloader = self._launch()
        except Exception as error:  # noqa: BLE001 - any launch failure is recoverable
            # No exc_info: the traceback would repeat the unredacted message.
            self.error = secrets_store.redact(f"{type(error).__name__}: {error}")
            self._next_attempt_at = self._clock() + self._cooldown
            logger.error("Browser launch failed: %s", self.error)
            return
        self.error = None

    def _finish(self) -> None:
        """Close the browser and answer every task still queued."""
        with self._lock:
            self._stopping = True  # no task can be queued after this
        self.initialized.set()
        self._close_downloader()
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
        except (PlaywrightError, OSError) as error:
            logger.error(
                "Closing the browser failed: %s", secrets_store.redact(str(error))
            )
        self.downloader = None

    def _retry_in(self) -> int | None:
        """Whole seconds until the next launch attempt, or None when not waiting."""
        if self.downloader is not None or self._stopping:
            return None
        return max(0, math.ceil(self._next_attempt_at - self._clock()))

    def _unavailable_message(self) -> str:
        """Text for a task refused because the browser is down."""
        return f"Browser unavailable ({self.error}); it is retried after a cooldown"

    def status(self) -> dict[str, str | int | None]:
        """Report the browser state for the UI.

        Returns:
            `state` ("starting", "available", "unavailable", "stopping" while
            work still runs after a stop request, or "stopped" once the thread
            has ended), the redacted last launch `error` (or None) and `retry_in`
            seconds until the next launch attempt (or None).
        """
        if self._stopping:
            state = "stopping" if self.is_alive() else "stopped"
        elif self.downloader is not None:
            state = "available"
        elif not self.initialized.is_set():
            state = "starting"
        else:
            state = "unavailable"
        return {"state": state, "error": self.error, "retry_in": self._retry_in()}

    def execute(
        self, func: Callable[..., T], *args: object, **kwargs: object
    ) -> T:
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
SCRAPER_ACTIVE = False

app.add_middleware(RequestGuardMiddleware)

# No CORS middleware on purpose: the UI is served by this app (same origin) and
# Obtainium is not a browser, so neither needs CORS headers. Without them a
# browser blocks other sites from reading responses or sending JSON PUT/DELETE.

# Setup templates directory
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "src" / "templates"))

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


# Cache for update status to prevent excessive polling load
UPDATE_CACHE: dict[str, Any] = {"timestamp": 0, "results": None}
UPDATE_LOCK = threading.Lock()

@app.get("/api/ui/updates")
def check_updates():
    """Checks each pipeline app for updates against remote sources."""
    global UPDATE_CACHE
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


def _redacted(error: BaseException) -> str:
    """Error text safe to log and show: secret values replaced."""
    return secrets_store.redact(f"{type(error).__name__}: {error}")


def _refresh_updates() -> dict:
    """Compute the update status of every pipeline app and cache it.

    Called with `UPDATE_LOCK` held.
    """
    global UPDATE_CACHE
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
                remote_versions, _, _ = get_supported_versions(package_id, config, quiet=True)
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
                if version.parse(latest_remote_app_str) > version.parse(current_patched_app_str):
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
                if remote_patch_versions.get(repo) != last_build.get("patch_versions", {}).get(repo):
                    has_update = True
                    break
            
        if not file_exists:
            has_update = True

        if last_build and last_build.get("patches") != selected_patches(app_info):
            has_update = True

        update_status[package_id] = {
            "has_update": has_update,
            "latest": latest_remote_app_str,
            "current": current_patched_app_str,
            "file_exists": file_exists,
            "icon_exists": icon_exists,
            "status": (
                "Error" if patch_list_error else app_info.get("status", "Unknown")
            ),
            "error": patch_list_error or app_info.get("error"),
        }

    # Stamped when the refresh ends: a slow one must not store an expired result.
    UPDATE_CACHE = {"timestamp": time.time(), "results": update_status}
    return update_status

# --- WEB UI ROUTES ---


@app.get("/", response_class=HTMLResponse)
async def web_ui(request: Request):
    """Serves the main Web UI with the initial data injected."""
    config = load_config()
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
            "metadata": metadata
        },
    )


@app.get("/api/ui/patches/{package_id}")
def get_patches_for_app(package_id: str):
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
    compatible_patches = []

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
                    if not isinstance(pkg, dict): continue
                    # ReVanced schema uses packageName, others use name
                    if pkg.get("packageName") == package_id or pkg.get("name") == package_id:
                        use = True
                        break
            elif not compat_pkgs:
                use = True

            if use:
                # Determine if it's enabled by default (check various schema fields)
                is_default = patch.get("default", patch.get("enabled_by_default", patch.get("use", True)))
                
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


class AddAppPayload(BaseModel):
    package_id: str
    name: str
    include_patches: List[str]


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

    def apply(config: dict) -> None:
        existing = next(
            (a for a in config.get("apps", []) if a["id"] == payload.package_id), None
        )
        new_app = {
            "id": payload.package_id,
            "name": payload.name,
            "obtainium_id": f"morphe_{payload.package_id.split('.')[-1]}",
            "include_patches": payload.include_patches,
            "exclude_patches": [],
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


def update_tools_task(config, force=False):
    global TOOLS_UPDATING, SYNC_ERROR
    TOOLS_UPDATING = True
    SYNC_ERROR = None
    try:
        update_tools(config, quiet=True, force=force)
    except GitHubRateLimitError as e:
        SYNC_ERROR = str(e)
    finally:
        TOOLS_UPDATING = False


def refresh_versions_task():
    """Background task to refresh the version cache for all apps in the pipeline."""
    global SCRAPER_ACTIVE
    if SCRAPER_ACTIVE:
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
        SCRAPER_ACTIVE = True
        SCRAPER_WORKER.execute(_do_refresh)
    except Exception as e:
        logger.error("Version refresh failed: %s", _redacted(e))
    finally:
        SCRAPER_ACTIVE = False


@app.post("/api/ui/add_source")
def add_source(payload: AddSourcePayload):
    repo = normalize_repo(payload.repo)
    if not repo:
        raise HTTPException(400, "Invalid repository. Use 'owner/repo' or a GitHub URL.")

    config = load_config()
    sources = config.get("sources", [])
    if any(s["repo"].lower() == repo.lower() for s in sources):
        raise HTTPException(400, f"Source {repo} already exists.")

    # Sync before saving so a broken repo never ends up half-added, and the
    # patches .json exists before the frontend re-renders the app list.
    # Success is judged by the files on disk: the sync returns False both on
    # failure and when already up to date.
    paths = get_source_paths(repo)
    try:
        check_and_download_release(repo, paths, force=True)
    except GitHubRateLimitError as e:
        raise HTTPException(429, str(e)) from e
    if not paths[".json"].exists():
        raise HTTPException(
            502,
            f"{repo} is missing the patches JSON (patches-list.json or patches.json).",
        )
    if not paths[".mpp"].exists():
        raise HTTPException(
            502, f"{repo} has no .mpp patch bundle in its latest release (or no releases)."
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
        if any(s["repo"].lower() == repo.lower() for s in current_sources):
            raise HTTPException(400, f"Source {repo} already exists.")
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
    file: UploadFile = File(...)
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
    
    with open(file_path, "wb") as buffer:
        content = await file.read()
        buffer.write(content)
    
    from src.patcher import extract_app_metadata
    extract_app_metadata(file_path, package_id)
        
    return {"status": "success", "message": f"Uploaded {file.filename} as {file_path.name}"}


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
def sync_discovery(background_tasks: BackgroundTasks):
    """Queue a forced tool and source sync and return the current app list."""
    config = load_config()
    background_tasks.add_task(update_tools_task, config, True)
    return {"apps": get_all_available_apps(config)}


class SaveSettingsPayload(BaseModel):
    server_url: str
    cron_schedule: str
    notification_urls: List[str] = []


def send_notification(title: str, message: str, config=None):
    if not config:
        config = load_config()
    
    urls = config.get("settings", {}).get("notification_urls", [])
    if not urls:
        return
        
    apobj = apprise.Apprise()
    for url in urls:
        apobj.add(url)
        
    apobj.notify(
        body=message,
        title=title,
    )


@app.post("/api/ui/test_notification")
def test_notification(payload: SaveSettingsPayload):
    if not payload.notification_urls:
        return {"status": "error", "message": "No notification URLs provided"}
        
    apobj = apprise.Apprise()
    for url in payload.notification_urls:
        apobj.add(url)
        
    success = apobj.notify(
        body="This is a test notification from Morphe Builder.",
        title="Morphe Builder Test",
    )
    return {"status": "success" if success else "error"}


CRON_LOCK = threading.Lock()


@app.post("/api/ui/save_settings")
def save_settings(payload: SaveSettingsPayload):
    def apply(config: dict) -> None:
        settings = config.setdefault("settings", {})
        settings["server_url"] = payload.server_url.rstrip("/")
        settings["cron_schedule"] = payload.cron_schedule
        settings["notification_urls"] = payload.notification_urls

    update_config(apply)
    # Schedule from the file as it is now, one caller at a time, so the last
    # scheduling call always sees the last saved settings.
    with CRON_LOCK:
        setup_cron_job(load_config())
    return {"status": "success"}


SECRET_SLUGS = {
    "github-token": secrets_store.GITHUB_TOKEN,
    "cloakbrowser-license-key": secrets_store.CLOAKBROWSER_LICENSE_KEY,
}
MAX_SECRET_BODY_BYTES = 4096


def _secret_status() -> dict[str, dict[str, object]]:
    """Return `{slug: {"set": bool, "source": str | None}}`, never a value."""
    try:
        sources = secrets_store.get_status()
    except OSError:
        logger.error("Could not read the secrets file")
        raise HTTPException(
            status_code=500, detail="Could not read the secrets file"
        ) from None
    return {
        slug: {"set": sources[name] is not None, "source": sources[name]}
        for slug, name in SECRET_SLUGS.items()
    }


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
    except secrets_store.SecretFromEnvironmentError:
        raise HTTPException(
            status_code=409, detail="Secret is set by the environment"
        ) from None
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


@app.put("/api/ui/secrets/{slug}")
async def put_secret(slug: str, request: Request):
    """Store a secret from `{"value": "..."}`; 409 if the environment sets it."""
    name = _secret_name(slug)
    value = await _read_secret_value(request)
    return await run_in_threadpool(_change_secret, name, value)


@app.delete("/api/ui/secrets/{slug}")
def delete_secret(slug: str):
    """Remove a stored secret; 409 if the environment sets it."""
    return _change_secret(_secret_name(slug), None)


class Tee(io.TextIOBase):
    def __init__(self, *files):
        self.files = files

    def write(self, obj):
        for f in self.files:
            f.write(obj)
            f.flush()
        return len(obj)


class TriggerBuildPayload(BaseModel):
    package_ids: List[str] | None = None


def build_task(package_ids=None, is_cron=False):
    global BUILD_STATUS, SCRAPER_ACTIVE
    
    current_time_str = time.strftime("%Y-%m-%d %H:%M")
    if is_cron and BUILD_STATUS.get("last_run_minute") == current_time_str:
        return

    if BUILD_STATUS["in_progress"]:
        print("[Cron] Build already in progress, skipping...")
        return

    def _do_build(scraper):
        global SCRAPER_ACTIVE
        BUILD_STATUS["in_progress"] = True
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
                    send_notification("Apps Patched Successfully", f"Updated: {', '.join(patched_names)}")
                elif not is_cron:
                    send_notification("Build Finished", "No updates or build failed.")
            except Exception as e:
                print(f"\n[FATAL ERROR] {e}")
                BUILD_STATUS["message"] = f"Failed: {e}"
                send_notification("Build Failed", f"Pipeline error: {e}")
            finally:
                sys.stdout = old_stdout
                # Invalidate update cache when build finishes
                global UPDATE_CACHE
                UPDATE_CACHE["timestamp"] = 0

    try:
        SCRAPER_ACTIVE = True
        SCRAPER_WORKER.execute(_do_build)
    except Exception as e:
        # Raised outside the build body (browser down): nothing else reports it.
        message = _redacted(e)
        logger.error("Build task failed: %s", message)
        BUILD_STATUS["message"] = f"Failed: {message}"
    finally:
        BUILD_STATUS["in_progress"] = False
        SCRAPER_ACTIVE = False


@app.post("/api/ui/trigger_build")
def trigger_build(payload: TriggerBuildPayload):
    if BUILD_STATUS["in_progress"]:
        return {"status": "Build already in progress"}

    # Invalidate cache to ensure UI shows correct status after trigger
    global UPDATE_CACHE
    UPDATE_CACHE["timestamp"] = 0

    # Use an empty list if None is sent to signify "Build All" but manual
    pkg_ids = payload.package_ids if payload.package_ids is not None else []
    
    # Use scheduler instead of BackgroundTasks to ensure it runs in a clean thread
    # for Playwright compatibility.
    scheduler.add_job(build_task, id=f"manual_build_{int(time.time())}", kwargs={"package_ids": pkg_ids, "is_cron": False})
    
    return {"status": "Build started"}


@app.get("/api/ui/build_status")
def get_build_status():
    return BUILD_STATUS


@app.get("/api/ui/tools_status")
def get_tools_status():
    return {
        "updating": TOOLS_UPDATING, 
        "sync_error": SYNC_ERROR,
        "diagnostics": DIAGNOSTICS,
        "browser": SCRAPER_WORKER.status(),
        "data_dir": str(DATA_DIR)
    }


@app.get("/api/ui/logs")
def get_logs():
    if not LOG_PATH.exists():
        return {"logs": ""}
    # Use newline="" to prevent \r being converted to \n
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace", newline="") as f:
        return {"logs": f.read()}


# --- OBTAINIUM ROUTES ---


@app.get("/download/{package_name}")
def download_app_by_name(package_name: str):
    """Serves the latest patched APK for a given package name or exact filename."""
    print(f"[Download] Request for: {package_name}")

    # Priority 1: Exact filename match in patched directory
    file_path = DATA_DIR / "apks" / "patched" / package_name
    if file_path.exists() and file_path.is_file():
        print(f"[Download] Found via exact match: {file_path}")
        return FileResponse(file_path, media_type="application/vnd.android.package-archive")

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
            print(f"[Download] Found newest for {base_name}: {newest}")
            return FileResponse(
                newest, media_type="application/vnd.android.package-archive"
            )

    raise HTTPException(
        status_code=404, detail=f"No patched APK matches {package_name}"
    )

@app.get("/view/{package_id}", response_class=HTMLResponse)
async def view_app(package_id: str, request: Request):
    """Serves a minimal HTML page for Obtainium to parse."""
    config = load_config()
    # Try to find by package ID first, then fallback to obtainium_id
    app_info = next((a for a in config.get("apps", []) if a["id"] == package_id), None)
    if not app_info:
        app_info = next((a for a in config.get("apps", []) if a.get("obtainium_id") == package_id), None)
        
    if not app_info:
        print(f"[View] App not found for ID: {package_id}")
        return HTMLResponse("App not found in pipeline", status_code=404)
        
    last_build = app_info.get("last_successful_build", {})
    current_version = last_build.get("combined_version") or last_build.get("app_version", "Unknown")
    
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
            filename = f"{app_info['id']}-patched.apk" # Legacy fallback
    
    patch_versions = last_build.get("patch_versions", {})
    
    # Just show first few patch versions if many exist
    patch_version_str = ", ".join([f"{repo.split('/')[-1]}:{v}" for repo, v in patch_versions.items()])
        
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
            "download_url": download_url
        }
    )


@app.get("/api/apps/{obtainium_id}")
def get_single_app(obtainium_id: str, request: Request):
    """Provides Obtainium metadata for a specific application."""
    print(f"[Obtainium] Request for ID: {obtainium_id}")
    config = load_config()
    settings = config.get("settings", {})
    base_url = settings.get("server_url", str(request.base_url).rstrip("/"))
    
    app_info = next((a for a in config.get("apps", []) if a.get("obtainium_id") == obtainium_id), None)
    if not app_info:
        print(f"[Obtainium] App with ID {obtainium_id} not found in config.")
        raise HTTPException(status_code=404, detail="App not found")
        
    last_build = app_info.get("last_successful_build", {})
    version = last_build.get("combined_version") or last_build.get("app_version", "Unknown")
    
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
    print(f"[Obtainium] Returning: {response}")
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
        version = last_build.get("combined_version") or last_build.get("app_version", "Unknown")
        
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
                "id": app_info["id"], # Match real package name
                "name": app_info["name"],
                "version": version,
                "download_url": f"{base_url}/download/{filename}",
            }
        )
    return {"apps": response_data}


def setup_cron_job(config):
    cron = config.get("settings", {}).get("cron_schedule")
    # Remove existing job if any
    for job in scheduler.get_jobs():
        job.remove()
    
    if cron:
        try:
            # For cron, we pass package_ids=None (default) and is_cron=True
            scheduler.add_job(build_task, CronTrigger.from_crontab(cron), id="periodic_build", kwargs={"is_cron": True})
            print(f"[Cron] Scheduled background build with: {cron}")
            
            # Refresh versions every hour to keep UI up to date
            scheduler.add_job(refresh_versions_task, CronTrigger(hour="*"), id="version_refresh")
            print("[Cron] Scheduled hourly version cache refresh")
        except Exception as e:
            print(f"[Cron] Failed to schedule: {e}")


@app.on_event("startup")
async def startup_event():
    global DIAGNOSTICS
    ensure_data_dirs()
    config = load_config()
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
    finishes the build before it closes the browser. If the build outlives
    `SHUTDOWN_JOIN_SECONDS` the daemon thread ends with the process.
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
