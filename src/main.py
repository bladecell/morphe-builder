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
from src.patcher import get_supported_versions, get_all_available_apps, run_pipeline, get_source_paths, update_tools, get_current_version, check_dependencies, DATA_DIR
from packaging import version
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

PROJECT_ROOT = Path(__file__).parent.parent
app = FastAPI()
scheduler = AsyncIOScheduler()

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
DIAGNOSTICS = {"java": False, "apkeep": False}
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


@app.get("/api/ui/updates")
def check_updates():
    """Checks each pipeline app for updates against remote sources."""
    config = load_config()
    update_status = {}
    
    # Check which apps actually have patched files existing
    patched_dir = DATA_DIR / "apks" / "patched"
    icons_dir = DATA_DIR / "apks" / "icons"
    
    for app_info in config.get("apps", []):
        package_id = app_info["id"]
        remote_versions = get_supported_versions(package_id, config)
        
        # Check if file exists
        file_exists = (patched_dir / f"{package_id}-patched.apk").exists()
        icon_exists = (icons_dir / f"{package_id}.png").exists()

        latest_remote_str = remote_versions[0] if remote_versions else "Unknown"
        current_local_str = get_current_version(package_id)
        
        has_update = False
        if remote_versions and current_local_str:
            try:
                if version.parse(latest_remote_str) > version.parse(current_local_str):
                    has_update = True
            except:
                pass
        elif remote_versions:
            has_update = True
            
        update_status[package_id] = {
            "has_update": has_update,
            "latest": latest_remote_str,
            "current": current_local_str or "None",
            "file_exists": file_exists,
            "icon_exists": icon_exists
        }
            
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
        "index.html",
        {
            "request": request, 
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
                if any(pkg.get("name") == package_id for pkg in compat_pkgs):
                    use = True
            elif not compat_pkgs:
                use = True

            if use:
                # Avoid duplicates from different sources
                if not any(p["name"] == patch.get("name") for p in compatible_patches):
                    compatible_patches.append(
                        {
                            "name": patch.get("name"),
                            "description": patch.get(
                                "description", "No description available."
                            ),
                            "enabled_by_default": patch.get("use", True),
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
    global BUILD_STATUS
    
    # Safety: Prevent scheduled builds from triggering multiple times in the same minute
    current_time_str = time.strftime("%Y-%m-%d %H:%M")
    if is_cron and BUILD_STATUS.get("last_run_minute") == current_time_str:
        return

    if BUILD_STATUS["in_progress"]:
        print("[Cron] Build already in progress, skipping...")
        return

    BUILD_STATUS["in_progress"] = True
    BUILD_STATUS["message"] = "Starting..."
    if is_cron:
        BUILD_STATUS["last_run_minute"] = current_time_str

    # Ensure log directory exists
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(LOG_PATH, "w", buffering=1, encoding="utf-8", newline="") as log_file:
        old_stdout = sys.stdout
        sys.stdout = Tee(sys.stdout, log_file)
        try:
            # Pass quiet=True if it's a cron build
            patched_ids = run_pipeline(package_ids, quiet=is_cron)
            BUILD_STATUS["message"] = "Finished"
            BUILD_STATUS["last_run"] = time.strftime("%H:%M:%S")
            
            # Get info for notification
            config = load_config()
            all_apps = {a["id"]: a["name"] for a in config.get("apps", [])}
            
            # Correct logic: only notify success if apps were ACTUALLY patched successfully
            if not patched_ids:
                if not is_cron: # Manual build
                    send_notification(
                        "Build Finished", 
                        "The build process completed, but no apps required patching (already up to date)."
                    )
                return

            patched_names = [all_apps.get(pid, pid) for pid in patched_ids]
            names_str = ", ".join(patched_names)
            
            send_notification(
                "Apps Patched Successfully", 
                f"The following apps were updated/patched: {names_str}"
            )
        except Exception as e:
            error_msg = str(e)
            print(f"\n[FATAL ERROR] {error_msg}")
            BUILD_STATUS["message"] = f"Failed: {error_msg}"
            send_notification(
                "Build Failed", 
                f"The pipeline build failed: {error_msg}"
            )
        finally:
            sys.stdout = old_stdout
            BUILD_STATUS["in_progress"] = False


@app.post("/api/ui/trigger_build")
def trigger_build(payload: TriggerBuildPayload, background_tasks: BackgroundTasks):
    if BUILD_STATUS["in_progress"]:
        return {"status": "Build already in progress"}

    # Use an empty list if None is sent to signify "Build All" but manual
    pkg_ids = payload.package_ids if payload.package_ids is not None else []
    background_tasks.add_task(build_task, package_ids=pkg_ids, is_cron=False)
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
    """Serves the latest patched APK for a given package name."""
    print(f"[Download] Request for: {package_name}")

    # Clean package name to handle .apk extension if present
    base_name = package_name.replace("-patched.apk", "").replace(".apk", "")

    # Priority 1: Exact filename match
    file_path = DATA_DIR / "apks" / "patched" / package_name
    if file_path.exists() and file_path.is_file():
        print(f"[Download] Found via exact match: {file_path}")
        return FileResponse(file_path, media_type="application/vnd.android.package-archive")

    # Priority 2: Inferred filename
    inferred_path = DATA_DIR / "apks" / "patched" / f"{base_name}-patched.apk"
    if inferred_path.exists() and inferred_path.is_file():
        print(f"[Download] Found via inferred path: {inferred_path}")
        return FileResponse(inferred_path, media_type="application/vnd.android.package-archive")

    # Search for any file starting with base_name
    print(f"[Download] File not found. Searching for glob: {base_name}* in {DATA_DIR}/apks/patched")
    try:
        patched_dir = DATA_DIR / "apks" / "patched"
        if patched_dir.exists():
            matches = list(patched_dir.glob(f"{base_name}*"))
            if matches:
                print(f"[Download] Found via glob: {matches[0]}")
                return FileResponse(matches[0], media_type="application/vnd.android.package-archive")
    except Exception as e:
        print(f"[Download] Glob search failed: {e}")

    print(f"[Download] FAILED. Searched: {file_path} and {inferred_path}")
    return {"error": "File not found", "searched": [str(file_path), str(inferred_path)]}

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
        
    try:
        supported = get_supported_versions(app_info["id"], config)
        current_version = supported[0] if supported else "Unknown"
    except:
        current_version = "Unknown"
        
    # Build absolute URL for the download link to help some parsers
    settings = config.get("settings", {})
    base_url = settings.get("server_url", str(request.base_url).rstrip("/"))
    download_url = f"{base_url}/download/{app_info['id']}-patched.apk"
        
    return templates.TemplateResponse(
        "obtainium.html",
        {
            "request": request,
            "app": app_info,
            "version": current_version,
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
        
    try:
        supported = get_supported_versions(app_info["id"], config)
        version = supported[0] if supported else "Unknown"
    except:
        version = "Unknown"

    # CRITICAL: Obtainium needs the 'id' to match the actual Android Package Name
    # to avoid the "could not get ID from apk" error.
    response = {
        "id": app_info["id"], 
        "name": app_info["name"],
        "version": version,
        "download_url": f"{base_url}/download/{app_info['id']}-patched.apk",
    }
    print(f"[Obtainium] Returning: {response}")
    return response


@app.get("/api/apps")
def get_apps(request: Request):
    config = load_config()
    settings = config.get("settings", {})
    base_url = settings.get("server_url", str(request.base_url).rstrip("/"))
    
    response_data = []
    for app_info in config.get("apps", []):
        try:
            supported = get_supported_versions(app_info["id"], config)
            version = supported[0] if supported else "Unknown"
        except:
            version = "Unknown"

        response_data.append(
            {
                "id": app_info["id"], # Match real package name
                "name": app_info["name"],
                "version": version,
                "download_url": f"{base_url}/download/{app_info['id']}-patched.apk",
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
        except Exception as e:
            print(f"[Cron] Failed to schedule: {e}")


@app.on_event("startup")
async def startup_event():
    global DIAGNOSTICS
    ensure_data_dirs()
    config = load_config()
    setup_cron_job(config)
    DIAGNOSTICS = check_dependencies(config)
    
    # Auto-sync on first run if tools are missing
    cli_jar = DATA_DIR / config["tools"]["cli_jar"]
    apkeditor_jar = DATA_DIR / config["tools"]["apkeditor_jar"]
    if not cli_jar.exists() or not apkeditor_jar.exists():
        print("[Init] Tools missing, triggering initial sync...")
        update_tools_task(config)
        
    scheduler.start()
