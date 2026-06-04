from fastapi import FastAPI, Request, BackgroundTasks, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yaml
import json
import sys
import io
import time
import apprise
from typing import List
from pathlib import Path
from src.patcher import get_supported_versions, get_all_available_apps, get_source_paths, update_tools, get_current_version, check_dependencies, DATA_DIR
from packaging import version
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import threading
import asyncio
import logging
import queue

# Silence verbose Uvicorn access logs
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

PROJECT_ROOT = Path(__file__).parent.parent
app = FastAPI()
scheduler = BackgroundScheduler()

# Global worker to manage a persistent downloader instance
class ScraperWorker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="ScraperWorker")
        self.task_queue = queue.Queue()
        self.downloader = None
        self.error = None
        self.initialized = threading.Event()

    def run(self):
        # Playwright Sync API requires NO event loop in the thread
        try:
            import asyncio
            asyncio.set_event_loop(None)
        except:
            pass
        
        try:
            from src.patcher import ApkMirror_Downloader
            print("[Worker] Initializing persistent ApkMirror_Downloader...")
            self.downloader = ApkMirror_Downloader()
            self.initialized.set()
        except Exception as e:
            print(f"[Worker] Failed to initialize downloader: {e}")
            self.error = e
            self.initialized.set()
            return

        while True:
            task = self.task_queue.get()
            func, args, kwargs, result_queue = task
            if func == "STOP":
                if self.downloader:
                    self.downloader.close()
                break

            try:
                # Pass downloader as a keyword argument to keep positional args clean
                res = func(*args, scraper=self.downloader, **kwargs)
                if result_queue: result_queue.put((res, None))
            except Exception as e:
                if result_queue: result_queue.put((None, e))
            finally:
                self.task_queue.task_done()


    def execute(self, func, *args, **kwargs):
        """Dispatches a function to be run in the worker thread."""
        if not self.initialized.is_set():
            self.initialized.wait(timeout=30)
        
        if self.error:
            raise self.error
            
        res_q = queue.Queue()
        self.task_queue.put((func, args, kwargs, res_q))
        res, err = res_q.get()
        if err:
            raise err
        return res

    def shutdown(self):
        self.task_queue.put(("STOP", (), {}, None))

SCRAPER_WORKER = ScraperWorker()
SCRAPER_ACTIVE = False

# Enable CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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

DEFAULT_CONFIG = {
    "repositories": {
        "cli": "MorpheApp/morphe-cli",
        "apkeditor": "REAndroid/APKEditor"
    },
    "sources": [
        {"name": "Morphe Patches", "repo": "MorpheApp/morphe-patches", "active": True}
    ],
    "tools": {
        "cli_jar": "bin/morphe-cli.jar",
        "apkeditor_jar": "bin/APKEditor.jar"
    },
    "apps": [],
    "settings": {
        "server_url": "http://localhost:8000",
        "cron_schedule": "0 */4 * * *",
        "notification_urls": []
    }
}


def ensure_data_dirs():
    """Ensure all required subdirectories exist in the DATA_DIR."""
    (DATA_DIR / "bin").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "apks" / "raw").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "apks" / "patched").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "apks" / "icons").mkdir(parents=True, exist_ok=True)


def load_config():
    config_path = DATA_DIR / "config.yaml"
    if not config_path.exists():
        ensure_data_dirs()
        source_config = PROJECT_ROOT / "config.yaml"
        if source_config.exists():
            import shutil
            shutil.copy(source_config, config_path)
            print(f"[Init] Seeded {config_path} from {source_config}")
        else:
            save_config(DEFAULT_CONFIG)
            print(f"[Init] Generated default {config_path}")
    
    with open(config_path, "r") as file:
        return yaml.safe_load(file)


def save_config(config_data):
    ensure_data_dirs()
    config_path = DATA_DIR / "config.yaml"
    with open(config_path, "w") as file:
        yaml.dump(config_data, file, sort_keys=False)


# Cache for update status to prevent excessive polling load
UPDATE_CACHE = {"timestamp": 0, "results": None}

@app.get("/api/ui/updates")
def check_updates():
    """Checks each pipeline app for updates against remote sources."""
    global UPDATE_CACHE
    now = time.time()
    
    # Return cached results if they are less than 60 seconds old
    if UPDATE_CACHE["results"] and (now - UPDATE_CACHE["timestamp"] < 60):
        return UPDATE_CACHE["results"]

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
        if pinned_version:
            # Skip APKMirror completely if pinned. 
            # get_supported_versions will return the pinned version via JSON check
            remote_versions, _, _ = get_supported_versions(package_id, config, quiet=True)
        else:
            # For unpinned apps, use the worker to find the newest from APKMirror
            # We use quiet=True here to avoid log spam from polling
            remote_versions, _, _ = SCRAPER_WORKER.execute(get_supported_versions, package_id, config, quiet=True)
        
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
        
        if latest_remote_app_str != "Unknown" and current_patched_app_str != "None":
            try:
                if version.parse(latest_remote_app_str) > version.parse(current_patched_app_str):
                    has_update = True
            except:
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
            
        update_status[package_id] = {
            "has_update": has_update,
            "latest": latest_remote_app_str,
            "current": current_patched_app_str,
            "file_exists": file_exists,
            "icon_exists": icon_exists,
            "status": app_info.get("status", "Unknown"),
            "error": app_info.get("error")
        }

    UPDATE_CACHE = {"timestamp": now, "results": update_status}
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
    config = load_config()
    compatible_patches = []

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

        patches_list = data.get("patches", []) if isinstance(data, dict) else data
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
                            "source": source["name"]
                        }
                    )
    return compatible_patches


class AddAppPayload(BaseModel):
    package_id: str
    name: str
    include_patches: List[str]


@app.post("/api/ui/add_app")
def add_app_to_config(payload: AddAppPayload):
    config = load_config()
    # Update existing or add new
    config["apps"] = [
        a for a in config.get("apps", []) if a["id"] != payload.package_id
    ]

    new_app = {
        "id": payload.package_id,
        "name": payload.name,
        "obtainium_id": f"morphe_{payload.package_id.split('.')[-1]}",
        "include_patches": payload.include_patches,
        "exclude_patches": [],
    }
    config.setdefault("apps", []).append(new_app)
    save_config(config)
    return {"status": "success"}


class DeleteAppPayload(BaseModel):
    package_id: str


@app.post("/api/ui/delete_app")
def delete_app_from_config(payload: DeleteAppPayload):
    config = load_config()
    config["apps"] = [
        a for a in config.get("apps", []) if a["id"] != payload.package_id
    ]
    save_config(config)
    return {"status": "success"}


class AddSourcePayload(BaseModel):
    repo: str


def update_tools_task(config):
    global TOOLS_UPDATING
    TOOLS_UPDATING = True
    try:
        update_tools(config, quiet=True)
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
                get_supported_versions(app["id"], config, scraper=scraper, force_refresh=True)

    try:
        SCRAPER_ACTIVE = True
        SCRAPER_WORKER.execute(_do_refresh)
    except Exception as e:
        print(f"[Cron] Version refresh failed: {e}")
    finally:
        SCRAPER_ACTIVE = False


@app.post("/api/ui/add_source")
def add_source(payload: AddSourcePayload, background_tasks: BackgroundTasks):
    config = load_config()
    sources = config.get("sources", [])
    
    # Sanitize repo: strip https://github.com/ if present
    repo = payload.repo.replace("https://github.com/", "").strip("/")
    
    if any(s["repo"] == repo for s in sources):
        return {"status": "Source already exists"}
    
    # Format name as 'User Patches'
    parts = repo.split("/")
    if len(parts) < 2:
        return {"status": "Invalid repository format"}
        
    user = parts[0]
    name = f"{user} Patches"
    
    sources.append({
        "name": name,
        "repo": repo,
        "active": True
    })
    save_config(config)
    background_tasks.add_task(update_tools_task, config)
    return {"status": "Source added, syncing in background"}


@app.post("/api/ui/toggle_source")
def toggle_source(payload: AddSourcePayload):
    config = load_config()
    for s in config.get("sources", []):
        if s["repo"] == payload.repo:
            s["active"] = not s.get("active", True)
            break
    save_config(config)
    return {"status": "success"}


@app.post("/api/ui/delete_source")
def delete_source(payload: AddSourcePayload):
    config = load_config()
    config["sources"] = [s for s in config.get("sources", []) if s["repo"] != payload.repo]
    save_config(config)
    return {"status": "success"}


@app.post("/api/ui/upload_apk")
async def upload_apk(
    package_id: str = Form(...),
    version_str: str = Form(...),
    file: UploadFile = File(...)
):
    raw_dir = DATA_DIR / "apks" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    
    # Sanitize version string
    safe_version = version_str.replace(" ", "_").strip()
    file_path = raw_dir / f"{package_id}-{safe_version}.apk"
    
    with open(file_path, "wb") as buffer:
        content = await file.read()
        buffer.write(content)
    
    from src.patcher import extract_app_metadata
    extract_app_metadata(file_path, package_id)
        
    return {"status": "success", "message": f"Uploaded {file.filename} as {file_path.name}"}


SOURCE_UPDATE_CACHE = {"timestamp": 0, "results": {}}

@app.get("/api/ui/source_updates")
def check_source_updates():
    """Checks for updates for all active patch sources with a 10-minute cache."""
    global SOURCE_UPDATE_CACHE
    now = time.time()
    
    # Return cached results if fresh (600 seconds = 10 mins)
    if now - SOURCE_UPDATE_CACHE["timestamp"] < 600:
        return SOURCE_UPDATE_CACHE["results"]

    config = load_config()
    results = {}
    config_changed = False
    
    local_versions = {}
    versions_file = DATA_DIR / "bin" / "versions.json"
    if versions_file.exists():
        with open(versions_file, "r") as f:
            try:
                local_versions = json.load(f)
            except:
                pass

    for source in config.get("sources", []):
        repo = source["repo"]
        current_tag = local_versions.get(repo)
        
        # Sync current version to config if missing or different
        if current_tag and source.get("version") != current_tag:
            source["version"] = current_tag
            config_changed = True

        try:
            api_url = f"https://api.github.com/repos/{repo}/releases/latest"
            resp = requests.get(api_url, timeout=5)
            if resp.status_code == 200:
                latest_tag = resp.json().get("tag_name")
                results[repo] = {
                    "latest": latest_tag,
                    "current": current_tag,
                    "has_update": latest_tag != current_tag
                }
        except:
            # Fallback to current only if request fails
            results[repo] = {"latest": current_tag, "current": current_tag, "has_update": False}

    if config_changed:
        save_config(config)
        
    SOURCE_UPDATE_CACHE = {"timestamp": now, "results": results}
    return results


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
def refresh_discovery(background_tasks: BackgroundTasks, sync: bool = False):
    config = load_config()
    if sync:
        background_tasks.add_task(update_tools_task, config)
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


@app.post("/api/ui/save_settings")
def save_settings(payload: SaveSettingsPayload):
    config = load_config()
    config.setdefault("settings", {})["server_url"] = payload.server_url.rstrip("/")
    config["settings"]["cron_schedule"] = payload.cron_schedule
    config["settings"]["notification_urls"] = payload.notification_urls
    save_config(config)
    setup_cron_job(config)
    return {"status": "success"}


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
        print(f"[Cron] Build task wrapper failed: {e}")
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
        "diagnostics": DIAGNOSTICS,
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

    # Priority 2: Search for any file starting with base_name (e.g., com.pkg.id-*.apk)
    print(f"[Download] File not found. Searching for glob: {base_name}* in {DATA_DIR}/apks/patched")
    try:
        patched_dir = DATA_DIR / "apks" / "patched"
        if patched_dir.exists():
            matches = list(patched_dir.glob(f"{base_name}*"))
            if matches:
                # Sort by modification time to get the newest if multiple exist
                matches.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                print(f"[Download] Found via glob: {matches[0]}")
                return FileResponse(matches[0], media_type="application/vnd.android.package-archive")
    except Exception as e:
        print(f"[Download] Glob search failed: {e}")

@app.get("/view/{package_id}", response_class=HTMLResponse)
async def view_app(package_id: str, request: Request):
    """Serves a minimal HTML page for Obtainium to parse."""
    config = load_config()
    # Try to find by package ID first, then fallback to obtainium_id
    app_info = next((a for a in config.get("apps", []) if a["id"] == package_id), None)
    if not app_info:
        app_info = next((a for a in config.get("apps", []) if a["obtainium_id"] == package_id), None)
        
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
    
    app_info = next((a for a in config.get("apps", []) if a["obtainium_id"] == obtainium_id), None)
    if not app_info:
        print(f"[Obtainium] App with ID {obtainium_id} not found in config.")
        return {"error": "App not found"}
        
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
    print("[Shutdown] Stopping scheduler...")
    scheduler.shutdown()
