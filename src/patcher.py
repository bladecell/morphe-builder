import requests
import yaml
import subprocess
import os
import json
import tempfile
import shutil
import sys
from pathlib import Path
from packaging import version

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT)))
VERSIONS_FILE = DATA_DIR / "bin" / "versions.json"


def load_local_versions():
    DATA_DIR.joinpath("bin").mkdir(parents=True, exist_ok=True)
    if VERSIONS_FILE.exists():
        with open(VERSIONS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_local_versions(versions):
    with open(VERSIONS_FILE, "w") as f:
        json.dump(versions, f, indent=4)


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
    results = {"java": False, "apkeep": False}
    java_bin = config.get("settings", {}).get("java_path", "java")
    
    try:
        subprocess.run([java_bin, "-version"], capture_output=True, check=True)
        results["java"] = True
    except:
        pass
        
    try:
        subprocess.run(["apkeep", "--version"], capture_output=True, check=True)
        results["apkeep"] = True
    except:
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


def get_supported_versions(package_name, config):
    print(f"Parsing supported versions for {package_name} across all sources...")
    supported_versions = set()

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
            if not compat_pkgs:
                continue

            if isinstance(compat_pkgs, dict):
                if package_name in compat_pkgs:
                    versions = compat_pkgs[package_name]
                    if isinstance(versions, list):
                        supported_versions.update([str(v).strip() for v in versions])
                    elif isinstance(versions, str):
                        supported_versions.add(versions.strip())
            elif isinstance(compat_pkgs, list):
                for pkg in compat_pkgs:
                    if isinstance(pkg, dict) and pkg.get("name") == package_name:
                        versions = pkg.get("versions") or []
                        if isinstance(versions, list):
                            supported_versions.update([str(v).strip() for v in versions])
                        elif isinstance(versions, str):
                            supported_versions.add(versions.strip())

    if not supported_versions:
        return []

    return sorted(list(supported_versions), key=lambda v: version.parse(v), reverse=True)


def extract_app_metadata(apk_path, package_id):
    """Extracts app name and icon from the APK using APKEditor and saves them locally."""
    try:
        java_bin = load_config().get("settings", {}).get("java_path", "java")
        apkeditor_path = DATA_DIR / "bin" / "APKEditor.jar"
        
        if not apkeditor_path.exists():
            return None

        result = subprocess.run(
            [java_bin, "-jar", str(apkeditor_path), "info", "-i", str(apk_path)],
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            print(f"    [!] APKEditor info failed for {package_id}")
            return None
            
        lines = result.stdout.splitlines()
        info = {}
        for line in lines:
            if "=" in line:
                key, value = line.split("=", 1)
                info[key.strip()] = value.strip().strip('"')

        app_name = info.get("AppName")
        icon_path = info.get("AppIcon")
        
        icon_dir = DATA_DIR / "apks" / "icons"
        icon_dir.mkdir(parents=True, exist_ok=True)
        
        # Extract icon
        if icon_path:
            with tempfile.TemporaryDirectory() as temp_dir:
                icon_ext = os.path.splitext(icon_path)[1].lower()
                target_icon_path = icon_path
                
                # If main icon is XML, try to find a PNG/WebP fallback
                if icon_ext == ".xml":
                    print(f"    [!] Main icon is XML, searching for fallbacks for {package_id}...")
                    try:
                        # List all files and look for PNG/WebP versions of the launcher icon
                        # ic_launcher.xml -> ic_launcher.png
                        base_name = os.path.basename(icon_path).replace(".xml", "")
                        result_list = subprocess.run(
                            ["unzip", "-l", str(apk_path)],
                            capture_output=True, text=True
                        )
                        
                        potential_icons = []
                        for line in result_list.stdout.splitlines():
                            parts = line.split()
                            if len(parts) >= 4:
                                path = parts[3]
                                # Look for PNG/WebP versions of the launcher icon
                                if (base_name in path or "ic_launcher" in path or "play_store" in path or "store_icon" in path or "logo" in path) and (path.endswith(".png") or path.endswith(".webp")):
                                    # Exclude tiny UI icons (usually < 5KB)
                                    try:
                                        size = int(parts[0])
                                        if size > 5000:
                                            potential_icons.append(path)
                                    except:
                                        potential_icons.append(path)
                        
                        if potential_icons:
                            # Prioritize: 1. Play store specific, 2. High density, 3. Round icons
                            potential_icons.sort(key=lambda x: (
                                "play_store" in x.lower() or "store_icon" in x.lower(),
                                "xxxhdpi" in x.lower(), 
                                "xxhdpi" in x.lower(),
                                "round" in x.lower()
                            ), reverse=True)
                            target_icon_path = potential_icons[0]
                            print(f"    [+] Found fallback icon: {target_icon_path}")
                    except:
                        pass

                # Extract the identified icon (if it's not still XML)
                if target_icon_path and not target_icon_path.endswith(".xml"):
                    try:
                        import shutil
                        subprocess.run(
                            ["unzip", "-j", str(apk_path), target_icon_path, "-d", temp_dir],
                            capture_output=True
                        )
                        extracted_icon = Path(temp_dir) / os.path.basename(target_icon_path)
                        if extracted_icon.exists():
                            shutil.move(extracted_icon, icon_dir / f"{package_id}.png")
                    except Exception as e:
                        print(f"    [!] Failed to extract icon file {target_icon_path}: {e}")
        
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
            "real_name": app_name or package_id,
            "version": info.get("VersionName", "Unknown")
        }
        
        with open(metadata_file, "w") as f:
            json.dump(metadata, f, indent=4)
            
        return metadata[package_id]
    except Exception as e:
        print(f"    [!] Failed to extract metadata for {package_id}: {e}")
        return None


def download_apk(package_name, target_version, final_path, tools_config, config):
    print(f"Downloading {package_name} v{target_version} using apkeep...")
    java_bin = config.get("settings", {}).get("java_path", "java")
    
    with tempfile.TemporaryDirectory() as temp_dir:
        command = [
            "apkeep",
            "-a",
            f"{package_name}@{target_version}",
            "-d",
            "apk-pure",
            temp_dir,
        ]
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            while True:
                char = process.stdout.read(1)
                if not char and process.poll() is not None:
                    break
                if char:
                    sys.stdout.write(char)
                    sys.stdout.flush()

            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, command)

            # Recursive search for APK/XAPK files in case apkeep creates subdirectories
            all_found_files = []
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    all_found_files.append(os.path.join(root, file))

            apk_files = [f for f in all_found_files if f.endswith(".apk")]
            xapk_files = [
                f for f in all_found_files if f.endswith(".xapk") or f.endswith(".apkm")
            ]

            if apk_files:
                temp_apk_path = apk_files[0]
                shutil.move(temp_apk_path, final_path)
                print(f"Successfully downloaded standalone APK to {final_path}")

            elif xapk_files:
                xapk_path = xapk_files[0]
                print(
                    f"Downloaded bundle ({os.path.basename(xapk_path)}). Merging splits via APKEditor..."
                )

                # Merge the .xapk into a single Universal APK
                apkeditor_path = DATA_DIR / tools_config["apkeditor_jar"]
                merge_command = [
                    java_bin,
                    "-jar",
                    str(apkeditor_path),
                    "m",  # 'm' stands for merge
                    "-i",
                    xapk_path,
                    "-o",
                    str(final_path),
                ]

                try:
                    process = subprocess.Popen(
                        merge_command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                    while True:
                        char = process.stdout.read(1)
                        if not char and process.poll() is not None:
                            break
                        if char:
                            sys.stdout.write(char)
                            sys.stdout.flush()
                    
                    if process.returncode != 0:
                        raise subprocess.CalledProcessError(
                            process.returncode, merge_command
                        )
                    print(f"Successfully merged Universal APK to {final_path}")
                except subprocess.CalledProcessError as e:
                    print(f"Failed to merge APKs. Exit code: {e.returncode}")
                    raise

            else:
                raise FileNotFoundError(
                    f"apkeep finished, but no .apk or .xapk found. Files in temp: {all_found_files}"
                )
            
            # Extract name and icon
            extract_app_metadata(final_path, package_name)

        except subprocess.CalledProcessError as e:
            print(f"\n--- apkeep Error Info ---")
            print(f"Exit Code: {e.returncode}")
            print("Check the logs above for the specific error output.")
            print(f"-------------------------\n")
            raise


def patch_app(app_config, tools_config, input_apk, output_apk, config):
    cli = DATA_DIR / tools_config["cli_jar"]
    java_bin = config.get("settings", {}).get("java_path", "java")
    
    command = [
        java_bin,
        "-jar",
        str(cli),
        "patch",
        "--continue-on-error",
        str(input_apk),
        "-o",
        str(output_apk),
    ]

    # Add all active source MPP files
    for source in config.get("sources", []):
        if source.get("active", True):
            mpp_path = get_source_paths(source["repo"])[".mpp"]
            if mpp_path.exists():
                command.extend(["--patches", str(mpp_path)])

    for p in app_config.get("include_patches", []):
        command.extend(["-e", p])
    for p in app_config.get("exclude_patches", []):
        command.extend(["-d", p])

    print(f"Running patcher for {app_config['name']}...")
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        while True:
            char = process.stdout.read(1)
            if not char and process.poll() is not None:
                break
            if char:
                sys.stdout.write(char)
                sys.stdout.flush()

        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, command)

        print(f"Successfully patched {app_config['name']} to {output_apk}")
    except subprocess.CalledProcessError as e:
        print(f"Patching failed for {app_config['name']}: {e}")


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


def run_pipeline(package_ids=None, quiet=False):
    config = load_config()
    was_updated = update_tools(config, quiet=quiet)

    raw_dir = DATA_DIR / "apks" / "raw"
    patched_dir = DATA_DIR / "apks" / "patched"
    raw_dir.mkdir(parents=True, exist_ok=True)
    patched_dir.mkdir(parents=True, exist_ok=True)

    apps_to_build = config["apps"]
    if package_ids:
        apps_to_build = [a for a in apps_to_build if a["id"] in package_ids]

    patched_apps = []

    for app in apps_to_build:
        try:
            supported = get_supported_versions(app["id"], config)
            if not supported:
                print(f"[!] No supported versions found for {app['id']}")
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
                    extract_app_metadata(raw_apk_path, app['id'])
                    break
                
                try:
                    download_apk(app["id"], v, raw_apk_path, config["tools"], config)
                    target_version = v
                    download_success = True
                    break
                except Exception as e:
                    print(f"    [!] Failed to download v{v}. Trying next version...")
                    if raw_apk_path and raw_apk_path.exists():
                        raw_apk_path.unlink(missing_ok=True) # Cleanup partial download
                    continue

            if not download_success:
                print(f"[x] Could not download any supported version for {app['id']}")
                continue

            patched_apk_path = patched_dir / f"{app['id']}-patched.apk"
            
            # CHECK IF WE SHOULD SKIP PATCHING
            if not was_updated and patched_apk_path.exists() and raw_apk_path.exists():
                # If patched file is newer than raw file, we already patched this version
                if patched_apk_path.stat().st_mtime > raw_apk_path.stat().st_mtime:
                    print(f"[\u2713] {app['name']} is already up to date (version {target_version}), skipping patch.")
                    continue

            patch_app(app, config["tools"], raw_apk_path, patched_apk_path, config)
            patched_apps.append(app["id"])
        except Exception as e:
            print(f"Error processing {app['id']}: {e}")
            
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
                pkgs_to_add = [pkg.get("name") for pkg in compat_pkgs if isinstance(pkg, dict) and "name" in pkg]

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
