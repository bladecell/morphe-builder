import requests
import re
import subprocess
import os
import json
import time
import tempfile
import hashlib
import shutil
import sys
import logging
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from packaging import version
from src.apkmirror_downloader import ApkMirror_Downloader, Arch, Dpi, BundleType

from src.config_store import DATA_DIR, load_config, update_config

logger = logging.getLogger(__name__)
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


def download_with_progress(url: str, path: Path) -> None:
    """Download a file with a progress bar, replacing `path` only when complete.

    The data goes to `<path>.part` first, so an interrupted download never leaves
    a truncated file at `path` and an existing file there stays intact.

    Args:
        url: URL to fetch.
        path: Final location of the file.

    Raises:
        requests.exceptions.RequestException: The request or the transfer failed.
        OSError: The file could not be written or moved into place.
    """
    partial_path = Path(f"{path}.part")
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
    response = requests.get(url, stream=True, timeout=30)
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
    """

    def __init__(self, reset_epoch: float | None = None) -> None:
        self.reset_epoch = reset_epoch
        if reset_epoch is None:
            when = "Try again later."
        else:
            reset = datetime.fromtimestamp(reset_epoch, tz=UTC)
            when = f"Resets at {reset:%Y-%m-%d %H:%M} UTC."
        super().__init__(f"GitHub API rate limit reached. {when}")


def _rate_limit_from_response(
    response: requests.Response,
) -> GitHubRateLimitError | None:
    """Builds a rate limit error if the response is a GitHub rate limit refusal.

    Args:
        response: Response from the GitHub API.

    Returns:
        The error to raise, or None when the response is not a rate limit
        (for example a 403 on a private repository).
    """
    if response.status_code not in (403, 429):
        return None
    headers = response.headers
    try:
        if headers.get("retry-after") is not None:
            return GitHubRateLimitError(time.time() + float(headers["retry-after"]))
        if headers.get("x-ratelimit-remaining") == "0":
            reset = headers.get("x-ratelimit-reset")
            return GitHubRateLimitError(float(reset) if reset else None)
    except ValueError:
        return GitHubRateLimitError()
    return None


def _try_download_json_candidates(repo_path, ref, output_path):
    """Tries candidate JSON filenames from a repo ref via raw.githubusercontent.
    Returns True on the first success."""
    for filename in JSON_FILENAME_CANDIDATES:
        raw_url = f"https://raw.githubusercontent.com/{repo_path}/{ref}/{filename}"
        try:
            download_with_progress(raw_url, output_path)
            return True
        except Exception:
            continue
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


def check_and_download_release(repo_path, extensions_to_paths, force=False):
    """Syncs a repo's latest release assets into the given local paths.

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
        failure, when already up to date, or when a repair fetched a list that
        is still unreadable.

    Raises:
        GitHubRateLimitError: GitHub refused the release lookup due to a rate limit.
    """
    api_url = f"https://api.github.com/repos/{repo_path}/releases/latest"
    local_versions = load_local_versions()
    current_tag = local_versions.get(repo_path)

    # Safeguard: Check if any required files are missing locally
    missing_files = [path for path in extensions_to_paths.values() if not path.exists()]
    list_unreadable = _patch_list_is_unreadable(repo_path, extensions_to_paths)

    # Throttle: skip the API call entirely if we checked recently and have files
    last_check = local_versions.get(f"{repo_path}#last_check")
    if (
        not force
        and not missing_files
        and not list_unreadable
        and last_check
        and (time.time() - last_check) < UPDATE_CHECK_INTERVAL
    ):
        return False

    release_data = None
    try:
        response = requests.get(api_url, timeout=30)
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
        rate_limit = _rate_limit_from_response(response)
        if rate_limit is not None:
            raise rate_limit
        response.raise_for_status()
        release_data = response.json()
    except requests.exceptions.RequestException as e:
        print(f"[x] Failed to fetch updates for {repo_path}: {e}")
        return False

    latest_tag = release_data.get("tag_name")

    if current_tag == latest_tag and not missing_files and not list_unreadable:
        print(f"[\u2713] {repo_path} is up to date ({current_tag}).")
        local_versions[f"{repo_path}#last_check"] = time.time()
        save_local_versions(local_versions)
        return False

    repair_only = False
    if current_tag != latest_tag:
        print(f"[!] Update found for {repo_path}: {current_tag} -> {latest_tag}")
    elif missing_files:
        print(f"[!] Missing local files for {repo_path}. Re-downloading...")
    else:
        print(f"[!] The patch list of {repo_path} is unreadable. Re-downloading it...")
        extensions_to_paths = {".json": extensions_to_paths[".json"]}
        repair_only = True

    all_found = True
    for ext, output_path in extensions_to_paths.items():
        found = False
        for asset in release_data.get("assets", []):
            if asset["name"].endswith(ext):
                asset_url = asset["browser_download_url"]
                print(f"    -> Downloading {asset['name']}...")
                try:
                    download_with_progress(asset_url, output_path)
                    found = True
                except Exception as e:
                    print(f"    [!] Failed to download {asset['name']}: {e}")
                break

        # Fallback: fetch the patches JSON from the repo at the release tag
        # (Morphe/ReVanced-style sources don't ship it as a release asset)
        if not found and ext == ".json":
            print(
                f"    -> JSON asset not found. Fetching patches JSON from source ({latest_tag})..."
            )
            if _try_download_json_candidates(repo_path, latest_tag, output_path):
                found = True

        if not found:
            print(f"    [!] Could not obtain {ext} for {repo_path}. Not marking as synced.")
            all_found = False

    if not all_found:
        return False

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


REPO_PATTERN = re.compile(r"^[\w.-]+/[\w.-]+$")


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
    return repo if REPO_PATTERN.match(repo) else None


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


def _apply_versions(config: dict, local_versions: dict) -> None:
    """Write installed source and tool versions into `config` in place.

    Args:
        config: Config dict to update.
        local_versions: Installed version per repo, from `load_local_versions`.
    """
    for source in config.get("sources", []):
        version_tag = local_versions.get(source["repo"])
        if version_tag:
            source["version"] = version_tag

    tools = config.setdefault("tools", {})
    repos = config.get("repositories", {})
    if repos.get("cli") in local_versions:
        tools["cli_version"] = local_versions[repos["cli"]]
    if repos.get("apkeditor") in local_versions:
        tools["apkeditor_version"] = local_versions[repos["apkeditor"]]


def selected_patches(app: dict) -> dict[str, list[str]]:
    """Return an app's patch selection in a form that compares regardless of order.

    Args:
        app: App entry from the config.

    Returns:
        Sorted included and excluded patch names under "include" and "exclude".
    """
    return {
        "include": sorted(app.get("include_patches") or []),
        "exclude": sorted(app.get("exclude_patches") or []),
    }


# Version of how patched APKs are put together. It is part of the version hash
# and of every build record. Bump it whenever a change to `patch_app` means
# APKs built before it are wrong or incomplete (for example selectors that
# were grouped under the wrong bundle): every app is then rebuilt once and
# gets a new version string. Do not bump it for ordinary changes.
BUILD_LAYOUT = 2


def build_combined_version(
    app_version: str,
    patch_versions: dict[str, str],
    selection: dict[str, list[str]],
) -> str:
    """Build the version string Obtainium sees for one patched APK.

    The string is `<app version>-<primary patch tag>-<hash>`. The primary tag is
    the version of the Morphe base source if it was used, else of the first
    used source. The hash covers every used source version and the patch
    selection, so it changes whenever one of them does and never otherwise.

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
    digest_input = json.dumps(
        {
            "layout": BUILD_LAYOUT,
            "patch_versions": patch_versions,
            "patches": selection,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(digest_input.encode()).hexdigest()[:8]
    return f"{app_version}-{primary_tag}-{digest}"


def is_build_current(
    last_build: dict,
    app_version: str,
    app: dict,
    patch_versions: dict[str, str],
    patched_dir: Path,
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
        and last_build.get("patches") == selected_patches(app)
        and all(patch_versions.get(r) == v for r, v in recorded_sources.items())
    )


def save_app_result(app_id: str, **fields: object) -> None:
    """Merge pipeline-owned fields into one app of the current config.

    Only `fields` are written, onto the file as it is now, so UI edits made during
    a build survive. If the app was deleted meanwhile, nothing is written.

    Args:
        app_id: Package id of the app the result belongs to.
        **fields: Result fields: status, error, last_successful_build.
    """

    def apply(config: dict) -> None:
        for app in config.get("apps", []):
            if app["id"] == app_id:
                app.update(fields)
                return

    update_config(apply)


def update_tools(config, quiet=False, force=False):
    """Syncs tool jars and active patch sources from GitHub.

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
    """
    if not quiet:
        print("--- Checking for Tool Updates ---")
    bin_dir = DATA_DIR / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    repos = config["repositories"]
    tools = config["tools"]
    
    any_updated = False
    rate_limit = None

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

        # Update each source
        for source in config.get("sources", []):
            if source.get("active", True):
                if not quiet:
                    print(f"Checking for updates for source: {source['name']} ({source['repo']})...")
                if check_and_download_release(
                    source["repo"], get_source_paths(source["repo"]), force=force
                ):
                    any_updated = True
    except GitHubRateLimitError as e:
        # Every remaining repo would hit the same limit: stop and report below.
        rate_limit = e

    # SYNC VERSIONS BACK TO CONFIG
    local_versions = load_local_versions()
    _apply_versions(config, local_versions)
    if any_updated:
        # Persist onto the file as it is now: `config` may be a stale snapshot.
        update_config(lambda current: _apply_versions(current, local_versions))

    if not quiet:
        print("---------------------------------\n")
    if rate_limit is not None:
        raise rate_limit
    return any_updated


BASE_PATCHES_REPO = "MorpheApp/morphe-patches"


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
    json_path = get_source_paths(repo)[".json"]
    try:
        with open(json_path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except json.JSONDecodeError as error:
        raise PatchListError(repo, f"invalid JSON: {error}") from error
    except UnicodeDecodeError as error:
        raise PatchListError(repo, "not valid UTF-8 text") from error
    except OSError as error:
        raise PatchListError(repo, error.strerror or type(error).__name__) from error
    patches = data.get("patches") if isinstance(data, dict) else data
    if not isinstance(patches, list):
        raise PatchListError(repo, "no list of patches")
    if not all(isinstance(patch, dict) for patch in patches):
        raise PatchListError(repo, "an entry is not a JSON object")
    if not all(isinstance(p.get("name"), str | None) for p in patches):
        raise PatchListError(repo, "a patch name is not text")
    return patches


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
    app_config = next((a for a in config.get("apps", []) if a["id"] == package_name), {})
    pinned_v = app_config.get("version")
    if pinned_v:
        if not quiet: print(f"    [+] Using pinned version from config for {package_name}: {pinned_v}")
        return [str(pinned_v)], None, None

    selected_patches = set(app_config.get("include_patches", []))
    
    # 1. Parse Recommended versions from active patch sources
    # POLICY: Only look at sources that are either "Morphe Patches" (base)
    # OR sources that actually provide one of the selected patches.
    versions_by_source = {} # repo -> set of versions
    providing_sources = [] # repos in config order

    for source in config.get("sources", []):
        if not source.get("active", True): continue
        repo = source["repo"]
        json_path = get_source_paths(source["repo"])[".json"]
        if not json_path.exists(): continue
        
        patches = load_patch_list(repo)

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
                        v_list = [
                            t.get("version")
                            for t in targets
                            if isinstance(t, dict)
                            and t.get("version")
                            and not t.get("isExperimental", False)
                        ]
            
            if v_list:
                if isinstance(v_list, list): s_versions.update([str(v).strip() for v in v_list if v])
                elif isinstance(v_list, str): s_versions.add(v_list.strip())
        
        if s_versions:
            versions_by_source[repo] = s_versions
            if has_selected_patch or is_base_repo(repo):
                providing_sources.append(repo)
    
    # If patches are selected, only use versions supported by the providers
    recommended_set = set()
    if selected_patches and providing_sources:
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
    else:
        # No patches selected yet (Discovery mode), merge all
        for s_versions in versions_by_source.values():
            recommended_set.update(s_versions)

    recommended = sorted(
        list(recommended_set), key=_safe_version_sort_key, reverse=True
    )
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
        newest = scraper.latest_version(package_name)
        if newest:
            versions = [newest]
            cache[package_name] = {"versions": versions, "timestamp": now}
            save_version_cache(cache)
            if not quiet: print(f"    [+] Found newest on APKMirror: {newest}")
            return versions, None, None
        return cached_remote, None, None
    except Exception as e:
        if raise_lookup_errors:
            raise VersionLookupError(f"{type(e).__name__}: {e}") from e
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
        info_result = subprocess.run(
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


def _compatible_patch_names(repo: str, package_id: str) -> set[str]:
    """List the patch names of one source that apply to a package.

    Args:
        repo: Source repo, `owner/repo`; its patch list must be on disk.
        package_id: Package the APK really has.

    Returns:
        Casefolded names of the patches compatible with the package.

    Raises:
        PatchListError: The patch list is missing, unreadable or malformed.
    """
    if not get_source_paths(repo)[".json"].exists():
        raise PatchListError(repo, "the file is missing")
    names: set[str] = set()
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
            names.add(name.casefold())
    return names


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
        PatchSelectionError: If a selected name is unknown to every bundle, or
            patches were selected and the CLI applied none. The output APK is
            removed in the second case. The message then quotes the last CLI
            output lines.
        PatchListError: If the patch list of a source that is needed, or of the
            base bundle used as fallback, is missing or unreadable.
    """
    cli = DATA_DIR / tools_config["cli_jar"]
    java_bin = config.get("settings", {}).get("java_path", "java")
    app_name = app_config["name"]
    input_package_id = _read_package_id(java_bin, input_apk, app_config["id"])

    included_mpps: dict[str, str] = {}  # path -> repo
    bundle_includes: dict[str, list[str]] = {}  # path -> names enabled via -e
    bundle_carries: dict[str, set[str]] = {}  # path -> casefolded names it carries
    include_order = list(dict.fromkeys(app_config.get("include_patches") or []))
    remaining = {name.casefold() for name in include_order}

    for source in config.get("sources", []):
        if not source.get("active", True) or not remaining:
            continue
        paths = get_source_paths(source["repo"])
        if not paths[".mpp"].exists():
            continue
        carries = _compatible_patch_names(source["repo"], input_package_id)
        found = remaining & carries
        if not found:
            continue
        key = str(paths[".mpp"])
        included_mpps[key] = source["repo"]
        bundle_includes.setdefault(key, []).extend(
            n for n in include_order if n.casefold() in found
        )
        bundle_carries[key] = carries
        remaining -= found

    if remaining or not included_mpps:
        default_mpp = get_source_paths(BASE_PATCHES_REPO)[".mpp"]
        if default_mpp.exists():
            default_carries = (
                _compatible_patch_names(BASE_PATCHES_REPO, input_package_id)
                if remaining
                else set()
            )
            found = remaining & default_carries
            key = str(default_mpp)
            included_mpps[key] = BASE_PATCHES_REPO
            bundle_includes.setdefault(key, []).extend(
                n for n in include_order if n.casefold() in found
            )
            bundle_carries.setdefault(key, set()).update(default_carries)
            remaining -= found

    if remaining:
        missing = [n for n in include_order if n.casefold() in remaining]
        raise PatchSelectionError(
            f"{app_name}: no patch bundle carries {', '.join(missing)} "
            f"for {input_package_id}"
        )

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
        process = subprocess.Popen(
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
                sys.stdout.write(line)
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

        if include_order and applied_count == 0:
            output_path.unlink(missing_ok=True)
            raise PatchSelectionError(
                f"{app_name}: no patch was applied although "
                f"{len(include_order)} were selected. Last CLI output: "
                f"{' | '.join(recent_lines) or '(none)'}"
            )

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
    except GitHubRateLimitError as e:
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
                    save_app_result(
                        app["id"], status="Error", error="No supported versions"
                    )
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
                    save_app_result(app["id"], status="Error", error="Download failed")
                    continue

                # ... patching logic ...
                last_build = app.get("last_successful_build", {})
                
                if not was_updated and is_build_current(
                    last_build, target_version, app, current_patch_versions, patched_dir
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
                            app, config["tools"], raw_apk_path, scratch_apk, config
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
                            selected_patches(app),
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
                        "patches": selected_patches(app),
                        "build_layout": BUILD_LAYOUT,
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
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
            entry_names = {}
            if isinstance(compat_pkgs, dict):
                pkgs_to_add = list(compat_pkgs.keys())
            elif isinstance(compat_pkgs, list):
                pkgs_to_add = []
                for pkg in compat_pkgs:
                    if not isinstance(pkg, dict): continue
                    # Prioritize packageName (ReVanced style), then name
                    p_id = pkg.get("packageName") or pkg.get("name")
                    if p_id:
                        pkgs_to_add.append(p_id)
                        # With a packageName present, 'name' is the display name
                        if pkg.get("packageName") and pkg.get("name"):
                            entry_names[str(p_id)] = pkg["name"]

            for pkg_id in pkgs_to_add:
                if not pkg_id: continue
                pkg_id = str(pkg_id)
                translated_name = entry_names.get(pkg_id) or app_names_map.get(pkg_id)
                # If we don't have a name yet, or we found a translated one, set it
                if pkg_id not in apps_data or translated_name:
                    apps_data[pkg_id] = translated_name or pkg_id

    # Return list of objects sorted by name
    result = [{"pkg": pkg, "name": name} for pkg, name in apps_data.items()]
    return sorted(result, key=lambda x: x["name"].lower())


if __name__ == "__main__":
    run_pipeline()
