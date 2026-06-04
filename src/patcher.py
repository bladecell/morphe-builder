import requests
import yaml
import subprocess
import os
import json
import time
import tempfile
import shutil
import sys
from pathlib import Path
from packaging import version
from src.apkmirror_downloader import ApkMirror_Downloader, Arch, Dpi, BundleType

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT)))
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


def download_with_progress(url, path):
    """Downloads a file with a text-based progress bar printed to stdout."""
    response = requests.get(url, stream=True)
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


def check_and_download_release(repo_path, extensions_to_paths):
    api_url = f"https://api.github.com/repos/{repo_path}/releases/latest"
    local_versions = load_local_versions()
    current_tag = local_versions.get(repo_path)

    # Safeguard: Check if any required files are missing locally
    missing_files = [path for path in extensions_to_paths.values() if not path.exists()]

    try:
        response = requests.get(api_url)
        response.raise_for_status()
        release_data = response.json()
        latest_tag = release_data.get("tag_name")

        if current_tag == latest_tag and not missing_files:
            print(f"[\u2713] {repo_path} is up to date ({current_tag}).")
            return False

        if current_tag != latest_tag:
            print(f"[!] Update found for {repo_path}: {current_tag} -> {latest_tag}")
        else:
            print(f"[!] Missing local files for {repo_path}. Re-downloading...")

        for ext, output_path in extensions_to_paths.items():
            found = False
            for asset in release_data.get("assets", []):
                if asset["name"].endswith(ext):
                    asset_url = asset["browser_download_url"]
                    print(f"    -> Downloading {asset['name']}...")
                    download_with_progress(asset_url, output_path)
                    found = True
                    break

            # Fallback for Morphe patches-list.json (not included in release assets)
            if not found and ext == ".json":
                print(
                    f"    -> JSON asset not found. Fetching patches-list.json from source ({latest_tag})..."
                )
                raw_url = f"https://raw.githubusercontent.com/{repo_path}/{latest_tag}/patches-list.json"
                try:
                    download_with_progress(raw_url, output_path)
                    found = True
                except:
                    print(f"    [!] Failed to download JSON from {raw_url}")

        local_versions[repo_path] = latest_tag
        save_local_versions(local_versions)
        return True

    except requests.exceptions.RequestException as e:
        print(f"[x] Failed to fetch updates for {repo_path}: {e}")
        return False


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
        subprocess.run([java_bin, "-version"], capture_output=True, check=True)
        results["java"] = True
    except:
        pass
        
    try:
        from src.apkmirror_downloader import ApkMirror_Downloader
        results["scraper"] = True
    except ImportError:
        pass
        
    return results


def update_tools(config, quiet=False):
    if not quiet:
        print("--- Checking for Tool Updates ---")
    bin_dir = DATA_DIR / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    repos = config["repositories"]
    tools = config["tools"]
    
    any_updated = False

    if check_and_download_release(repos["cli"], {".jar": DATA_DIR / tools["cli_jar"]}):
        any_updated = True

    # Download APKEditor
    if "apkeditor" in repos and "apkeditor_jar" in tools:
        if check_and_download_release(
            repos["apkeditor"], {".jar": DATA_DIR / tools["apkeditor_jar"]}
        ):
            any_updated = True

    # Update each source
    for source in config.get("sources", []):
        if source.get("active", True):
            if not quiet:
                print(f"Checking for updates for source: {source['name']} ({source['repo']})...")
            if check_and_download_release(source["repo"], get_source_paths(source["repo"])):
                any_updated = True

    # SYNC VERSIONS BACK TO CONFIG
    local_versions = load_local_versions()
    # Update source versions
    for source in config.get("sources", []):
        v = local_versions.get(source["repo"])
        if v:
            source["version"] = v
            
    # Update tool versions in config
    if "tools" not in config: config["tools"] = {}
    repos = config.get("repositories", {})
    if repos.get("cli") in local_versions:
        config["tools"]["cli_version"] = local_versions[repos["cli"]]
    if repos.get("apkeditor") in local_versions:
        config["tools"]["apkeditor_version"] = local_versions[repos["apkeditor"]]

    if any_updated:
        from src.main import save_config
        save_config(config)

    if not quiet:
        print("---------------------------------\n")
    return any_updated


def load_config():
    config_path = DATA_DIR / "config.yaml"
    # Fallback/Seed: if config doesn't exist in data_dir, copy from root
    if not config_path.exists() and (PROJECT_ROOT / "config.yaml").exists():
        import shutil
        shutil.copy(PROJECT_ROOT / "config.yaml", config_path)
    
    with open(config_path, "r") as file:
        return yaml.safe_load(file)


def get_supported_versions(package_name, config, scraper=None, force_refresh=False, quiet=False):
    """
    Fetches available versions for a package with prioritization:
    1. Pinned version in config (Return immediately)
    2. Newest Recommended version from patch sources (Filtered by selected patches)
    3. Newest version from APKMirror search (Fallback)
    Returns: (final_list, None, None)
    """
    app_config = next((a for a in config.get("apps", []) if a["id"] == package_name), {})
    pinned_v = app_config.get("version")
    if pinned_v:
        if not quiet: print(f"    [+] Using pinned version from config for {package_name}: {pinned_v}")
        return [str(pinned_v)], None, None

    selected_patches = set(app_config.get("include_patches", []))
    
    # 1. Parse Recommended versions from active patch sources
    # POLICY: Only look at sources that are either "Morphe Patches" (base)
    # OR sources that actually provide one of the selected patches.
    versions_by_source = {} # source_name -> set of versions
    providing_sources = set()

    for source in config.get("sources", []):
        if not source.get("active", True): continue
        s_name = source["name"]
        json_path = get_source_paths(source["repo"])[".json"]
        if not json_path.exists(): continue
        
        try:
            with open(json_path, "r") as f:
                data = json.load(f)
                patches = data.get("patches", []) if isinstance(data, dict) else data
                
                s_versions = set()
                has_selected_patch = False
                
                for p in patches:
                    p_name = p.get("name")
                    if p_name in selected_patches:
                        has_selected_patch = True
                    
                    compat = p.get("compatiblePackages")
                    if not compat: continue
                    
                    v_list = []
                    if isinstance(compat, dict) and package_name in compat:
                        v_list = compat[package_name]
                    elif isinstance(compat, list):
                        for pkg in compat:
                            if not isinstance(pkg, dict): continue
                            pkg_id = pkg.get("packageName") or pkg.get("name")
                            if pkg_id != package_name: continue
                            v_list = pkg.get("versions") or []
                            targets = pkg.get("targets")
                            if targets and isinstance(targets, list):
                                v_list = [t.get("version") for t in targets if t.get("version") and not t.get("isExperimental", False)]
                    
                    if v_list:
                        if isinstance(v_list, list): s_versions.update([str(v).strip() for v in v_list if v])
                        elif isinstance(v_list, str): s_versions.add(v_list.strip())
                
                if s_versions:
                    versions_by_source[s_name] = s_versions
                    if has_selected_patch or s_name == "Morphe Patches":
                        providing_sources.add(s_name)
        except: pass
    
    # If patches are selected, only use versions supported by the providers
    recommended_set = set()
    if selected_patches and providing_sources:
        # Collect versions from providing sources
        for s_name in providing_sources:
            if s_name in versions_by_source:
                # If we haven't picked a version yet, take the first provider's
                if not recommended_set:
                    recommended_set = versions_by_source[s_name]
                else:
                    # INTERSECT versions if multiple sources provide patches
                    # (Ensure we pick a version compatible with ALL selected sources)
                    intersect = recommended_set.intersection(versions_by_source[s_name])
                    if intersect:
                        recommended_set = intersect
                    else:
                        # If no intersection, fallback to the provider that isn't Morphe
                        if s_name != "Morphe Patches":
                            recommended_set = versions_by_source[s_name]
    else:
        # No patches selected yet (Discovery mode), merge all
        for s_versions in versions_by_source.values():
            recommended_set.update(s_versions)

    recommended = sorted(list(recommended_set), key=lambda v: version.parse(v), reverse=True)
    if recommended:
        if not quiet: print(f"    [+] Found {len(recommended)} compatible recommended versions. Using newest: {recommended[0]}")
        return [recommended[0]], None, None

    # 2. Fallback to newest from APKMirror
    cache = load_version_cache()
    entry = cache.get(package_name, {})
    now = time.time()
    cached_remote = entry.get("versions", [])
    
    if not force_refresh and entry and (now - entry.get("timestamp", 0) < 43200):
        if cached_remote: return cached_remote, None, None

    if not quiet: print(f"Fetching newest version for {package_name} from APKMirror...")
    close_scraper = False
    if scraper is None:
        scraper = ApkMirror_Downloader()
        close_scraper = True

    try:
        app_data = scraper.search(package_name=package_name, only_release=True)
        if app_data and app_data.version:
            versions = [app_data.version]
            cache[package_name] = {"versions": versions, "timestamp": now}
            save_version_cache(cache)
            if not quiet: print(f"    [+] Found newest on APKMirror: {app_data.version}")
            return versions, None, None
        return cached_remote, None, None
    except Exception as e:
        if not quiet: print(f"    [!] Scraper failed for {package_name}: {e}")
        return cached_remote, None, None
    finally:
        if close_scraper: scraper.close()


def extract_app_metadata(apk_path, package_id, scraper=None):
    """Extracts app name from the APK and icon from APKMirror Scraper."""
    try:
        java_bin = load_config().get("settings", {}).get("java_path", "java")
        apkeditor_path = DATA_DIR / "bin" / "APKEditor.jar"
        
        app_name = package_id
        version_name = "Unknown"

        if apkeditor_path.exists():
            result = subprocess.run(
                [java_bin, "-jar", str(apkeditor_path), "info", "-i", str(apk_path)],
                capture_output=True,
                text=True
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
            
            try:
                app_data = scraper.search(package_name=package_id, only_release=True)
                if app_data and app_data.icon_url and app_data.icon_url != "Unknown":
                    scraper.download_file(
                        target_url=app_data.icon_url,
                        dest_directory=str(icon_dir),
                        filename=f"{package_id}.png"
                    )
            finally:
                if close_scraper:
                    scraper.close()
        
        # Save metadata
        metadata_file = DATA_DIR / "bin" / "metadata.json"
        metadata = {}
        if metadata_file.exists():
            with open(metadata_file, "r") as f:
                try:
                    metadata = json.load(f)
                except:
                    pass
        
        metadata[package_id] = {
            "real_name": app_name,
            "version": version_name
        }
        
        with open(metadata_file, "w") as f:
            json.dump(metadata, f, indent=4)
            
        return metadata[package_id]
    except Exception as e:
        print(f"    [!] Failed to extract metadata for {package_id}: {e}")
        return None


def get_app_preferences(package_name, config):
    """Scans patch sources to find if the app has a preferred apkFileType (APK vs BUNDLE)."""
    for source in config.get("sources", []):
        if not source.get("active", True): continue
        json_path = get_source_paths(source["repo"])[".json"]
        if not json_path.exists(): continue
        try:
            with open(json_path, "r") as f:
                data = json.load(f)
                patches = data.get("patches", []) if isinstance(data, dict) else data
                for p in patches:
                    compat = p.get("compatiblePackages")
                    if not compat or not isinstance(compat, list): continue
                    for pkg in compat:
                        if not isinstance(pkg, dict): continue
                        p_id = pkg.get("packageName") or pkg.get("name")
                        if p_id == package_name:
                            ftype = pkg.get("apkFileType")
                            if ftype:
                                return ftype.upper()
        except: pass
    return None


def download_apk(package_name, target_version, final_path, tools_config, config, scraper=None):
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
                print(f"    [+] Source specifies standalone preference: {pref}. Prioritizing APK.")
                prefer_bundle = False
            elif any(x in pref for x in ["BUNDLE", "APKM", "XAPK"]):
                print(f"    [+] Source specifies bundle preference: {pref}. Prioritizing Bundle.")
                prefer_bundle = True
        else:
            print(f"    [~] No patch preference found. Using default architecture/bundle settings...")
            prefer_bundle = True # Default to bundle as APKEditor can merge it cleanly

        app_data = scraper.search(
            package_name=package_name,
            version=target_version,
            only_release=True,
            prefer_bundle=prefer_bundle
        )

        if not app_data:
            raise Exception(f"Could not find {package_name} v{target_version} on APKMirror")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_file_name = f"{package_name}_{target_version}_download.tmp"
            
            downloaded_path = scraper.download_file(
                target_url=app_data.download_url,
                dest_directory=temp_dir,
                filename=temp_file_name
            )
            
            if not downloaded_path or not os.path.exists(downloaded_path):
                raise Exception("Download failed or file not found.")

            # Download icon
            icon_dir = DATA_DIR / "apks" / "icons"
            icon_dir.mkdir(parents=True, exist_ok=True)
            if app_data.icon_url and app_data.icon_url != "Unknown":
                scraper.download_file(
                    target_url=app_data.icon_url,
                    dest_directory=str(icon_dir),
                    filename=f"{package_name}.png"
                )

            # Check if the downloaded file is a bundle by inspecting its zip contents
            import zipfile
            is_bundle_file = False
            try:
                with zipfile.ZipFile(downloaded_path, 'r') as z:
                    if any(name.endswith('.apk') for name in z.namelist()):
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
                    str(final_path),
                ]
                subprocess.run(merge_command, check=True)
                print(f"Successfully merged Universal APK to {final_path}")
            else:
                shutil.move(downloaded_path, final_path)
                print(f"Successfully downloaded standalone APK to {final_path}")

            # Extract name from APK as a final check/metadata update
            extract_app_metadata(final_path, package_name, scraper=scraper)

    finally:
        if close_scraper:
            scraper.close()


def patch_app(app_config, tools_config, input_apk, output_apk, config):
    cli = DATA_DIR / tools_config["cli_jar"]
    java_bin = config.get("settings", {}).get("java_path", "java")
    
    # Get the real package ID from the input APK to verify compatibility
    input_package_id = app_config["id"]
    try:
        apkeditor_path = DATA_DIR / "bin" / "APKEditor.jar"
        info_result = subprocess.run(
            [java_bin, "-jar", str(apkeditor_path), "info", "-i", str(input_apk)],
            capture_output=True, text=True
        )
        for line in info_result.stdout.splitlines():
            if "package=" in line:
                input_package_id = line.split("=", 1)[1].strip().strip('"')
                break
    except:
        pass

    # Map each selected patch name to a SINGLE source
    included_mpps = {} # path -> repo
    selected_patches = set(app_config.get("include_patches", []))
    remaining_patches = selected_patches.copy()

    for source in config.get("sources", []):
        if not source.get("active", True) or not remaining_patches:
            continue
        
        mpp_path = get_source_paths(source["repo"])[".mpp"]
        json_path = get_source_paths(source["repo"])[".json"]
        
        if not mpp_path.exists() or not json_path.exists():
            continue

        try:
            with open(json_path, "r") as f:
                source_data = json.load(f)
                patches_list = source_data.get("patches", []) if isinstance(source_data, dict) else source_data
                
                compatible_source_patches = set()
                for p in patches_list:
                    p_name = p.get("name")
                    comp = p.get("compatiblePackages")
                    is_compat = False
                    if isinstance(comp, dict) and input_package_id in comp: 
                        is_compat = True
                    elif isinstance(comp, list):
                        for cp in comp:
                            if not isinstance(cp, dict): continue
                            if cp.get("packageName") == input_package_id or cp.get("name") == input_package_id:
                                is_compat = True
                                break
                    elif not comp: 
                        is_compat = True
                        
                    if is_compat: 
                        compatible_source_patches.add(p_name)
                
                found_in_this_source = remaining_patches.intersection(compatible_source_patches)
                if found_in_this_source:
                    included_mpps[str(mpp_path)] = source["repo"]
                    remaining_patches -= found_in_this_source
        except Exception as e:
            print(f"    [!] Error analyzing source {source['name']}: {e}")
            pass

    if remaining_patches or not included_mpps:
        default_repo = "MorpheApp/morphe-patches"
        default_mpp = get_source_paths(default_repo)[".mpp"]
        if default_mpp.exists():
            included_mpps[str(default_mpp)] = default_repo

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

    for mpp_path in included_mpps.keys():
        command.extend(["--patches", mpp_path])

    for p in app_config.get("include_patches", []):
        command.extend(["-e", p])
    for p in app_config.get("exclude_patches", []):
        command.extend(["-d", p])

    print(f"Running patcher for {app_config['name']}...")
    has_severe_error = False
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if line:
                if "SEVERE:" in line:
                    has_severe_error = True
                sys.stdout.write(line)
                sys.stdout.flush()

        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, command)

        if has_severe_error:
            print(f"\n[!] {app_config['name']} patched with some SEVERE errors. Deleting failed APK.")
            if Path(output_apk).exists():
                Path(output_apk).unlink(missing_ok=True)
            return False, []

        print(f"Successfully patched {app_config['name']} to {output_apk}")
        return True, list(included_mpps.values())
    except Exception as e:
        print(f"Patching failed for {app_config['name']}: {e}")
        if Path(output_apk).exists():
            Path(output_apk).unlink(missing_ok=True)
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
    was_updated = update_tools(config, quiet=quiet)
    
    # Get current versions of all active patch sources
    current_patch_versions = get_patch_versions(config)

    raw_dir = DATA_DIR / "apks" / "raw"
    patched_dir = DATA_DIR / "apks" / "patched"
    raw_dir.mkdir(parents=True, exist_ok=True)
    patched_dir.mkdir(parents=True, exist_ok=True)

    apps_to_build = config["apps"]
    if package_ids:
        apps_to_build = [a for a in apps_to_build if a["id"] in package_ids]

    patched_apps = []
    
    close_scraper = False
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
                
                supported, _, _ = get_supported_versions(app["id"], config, scraper=scraper, force_refresh=True)
                
                # PINNED VERSION LOGIC:
                # ...
                pinned_version = app.get("version")
                if pinned_version:
                    print(f"[!] Pinned version {pinned_version} detected for {app['id']}. Ignoring other versions.")
                    supported = [pinned_version]

                if not supported:
                    print(f"[!] No supported versions found for {app['id']}")
                    app["status"] = "Error"
                    app["error"] = "No supported versions"
                    from src.main import save_config
                    save_config(config)
                    continue

                download_success = False
                target_version = None
                raw_apk_path = None

                for v in supported:
                    raw_apk_path = raw_dir / f"{app['id']}-{v}.apk"
                    if raw_apk_path.exists():
                        target_version = v
                        download_success = True
                        print(f"Raw APK already exists for {app['id']} v{v}, skipping download.")
                        extract_app_metadata(raw_apk_path, app['id'], scraper=scraper)
                        break
                    
                    try:
                        download_apk(app["id"], v, raw_apk_path, config["tools"], config, scraper=scraper)
                        target_version = v
                        download_success = True
                        break
                    except Exception as e:
                        print(f"    [!] Failed to download v{v}: {e}. Trying next version...")
                        if raw_apk_path and raw_apk_path.exists():
                            raw_apk_path.unlink(missing_ok=True) # Cleanup partial download
                        continue

                if not download_success:
                    # ...
                    print(f"[x] Could not download any supported version for {app['id']}")
                    app["status"] = "Error"
                    app["error"] = "Download failed"
                    from src.main import save_config
                    save_config(config)
                    continue

                # ... patching logic ...
                last_build = app.get("last_successful_build", {})
                
                # Helper for combined version string
                # Simple combined version: app_version-patch_version (using first available patch tag)
                patch_tag = "v0.0.0"
                if current_patch_versions:
                    # Prefer Morphe patches if available
                    morphe_v = next((v for r, v in current_patch_versions.items() if "morphe-patches" in r.lower()), None)
                    patch_tag = morphe_v if morphe_v else list(current_patch_versions.values())[0]
                
                combined_version = f"{target_version}-{patch_tag}"
                patched_apk_path = patched_dir / f"{app['id']}-{combined_version}.apk"

                if (not was_updated and
                    patched_apk_path.exists() and 
                    last_build.get("combined_version") == combined_version):
                    
                    print(f"[\u2713] {app['name']} is already up to date (Version: {combined_version}), skipping patch.")
                    continue

                # Clean up old patched versions for this app before building new one
                for old_file in patched_dir.glob(f"{app['id']}-*.apk"):
                    try:
                        old_file.unlink()
                    except:
                        pass

                success, used_repos = patch_app(app, config["tools"], raw_apk_path, patched_apk_path, config)
                if success:
                    patched_apps.append(app["id"])
                    
                    # Only save versions for the repos actually used for this app
                    relevant_patch_versions = {repo: current_patch_versions.get(repo, "Unknown") for repo in used_repos}
                    
                    # Update the app entry in our local config object
                    app["last_successful_build"] = {
                        "app_version": target_version,
                        "patch_versions": relevant_patch_versions,
                        "combined_version": combined_version,
                        "filename": patched_apk_path.name,
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    app["status"] = "Success"
                    app["error"] = None
                    from src.main import save_config
                    save_config(config)
                else:
                    app["status"] = "Failed"
                    app["error"] = "Patching failed with SEVERE errors"
                    from src.main import save_config
                    save_config(config)
            except Exception as e:
                print(f"Error processing {app['id']}: {e}")
                app["status"] = "Error"
                app["error"] = str(e)
                from src.main import save_config
                save_config(config)
    finally:
        if close_scraper:
            scraper.close()
            
    return patched_apps


def get_all_available_apps(config):
    """Scans all active source JSONs and returns a unique list of supported apps with translated names."""
    apps_data = {} # pkg_id -> name
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

        app_names_map = data.get("appNames", {})
        patches_list = data.get("patches", []) if isinstance(data, dict) else data
        for patch in patches_list:
            if not isinstance(patch, dict):
                continue
            compat_pkgs = patch.get("compatiblePackages")
            if not compat_pkgs:
                continue

            pkgs_to_add = []
            if isinstance(compat_pkgs, dict):
                pkgs_to_add = list(compat_pkgs.keys())
            elif isinstance(compat_pkgs, list):
                pkgs_to_add = []
                for pkg in compat_pkgs:
                    if not isinstance(pkg, dict): continue
                    # Prioritize packageName (ReVanced style), then name
                    p_id = pkg.get("packageName") or pkg.get("name")
                    if p_id: pkgs_to_add.append(p_id)

            for pkg_id in pkgs_to_add:
                if not pkg_id: continue
                pkg_id = str(pkg_id)
                translated_name = app_names_map.get(pkg_id)
                # If we don't have a name yet, or we found a translated one, set it
                if pkg_id not in apps_data or translated_name:
                    apps_data[pkg_id] = translated_name or pkg_id

    # Return list of objects sorted by name
    result = [{"pkg": pkg, "name": name} for pkg, name in apps_data.items()]
    return sorted(result, key=lambda x: x["name"].lower())


if __name__ == "__main__":
    run_pipeline()
