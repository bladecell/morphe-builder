"""A source keeps a release `.json` as its patch list only if it is one."""

import json
from pathlib import Path
from typing import ClassVar

import pytest
import requests

from src import patcher

REPO = "SysAdminDoc/Hushfacebook"
TAG = "v0.7.1"
DOWNLOAD = "https://github.com/SysAdminDoc/Hushfacebook/releases/download/v0.7.1/"
RAW = f"https://raw.githubusercontent.com/{REPO}/{TAG}/"

SBOM = json.dumps(
    {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:1",
        "version": 1,
        "metadata": {},
        "components": [],
        "dependencies": [],
    }
)
TOOLING = json.dumps({"tools": ["x"]})
RECEIPT = json.dumps({"release": "0.7.1"})
REAL_LIST = json.dumps(
    {
        "version": "0.7.1",
        "appNames": {"com.facebook.katana": "Facebook"},
        "patches": [
            {
                "name": "Hide ads",
                "use": False,
                "compatiblePackages": {"com.facebook.katana": ["1.0"]},
            },
            {"name": "Hide reels", "use": False, "compatiblePackages": {}},
        ],
    }
)
OTHER_LIST = json.dumps({"patches": [{"name": "Other", "compatiblePackages": {}}]})

# Assets of the Hushfacebook v0.7.1 release, in the order GitHub returned them.
HUSHFACEBOOK_ASSETS = [
    ("patches-0.7.1.cdx.json", 7765),
    ("patches-0.7.1.mpp", 2_000_000),
    ("patches-0.7.1.tooling.json", 963_787),
    ("release-receipt-0.7.1.json", 1_500),
    ("SHA256SUMS.txt", 400),
]
BASE_ASSETS = [("patches-1.0.mpp", 2_000_000)]


class Release:
    """GitHub release lookup response."""

    status_code = 200
    headers: ClassVar[dict[str, str]] = {}

    def __init__(self, assets: list[tuple[str, int]], tag: str = TAG) -> None:
        self.assets = assets
        self.tag = tag

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "tag_name": self.tag,
            "assets": [
                {
                    "name": name,
                    "size": size,
                    "browser_download_url": DOWNLOAD + name,
                }
                for name, size in self.assets
            ],
        }


class Network:
    """Serves release files by URL and records every download."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.urls: list[str] = []

    def download(self, url: str, path: Path) -> None:
        self.urls.append(url)
        if url not in self.files:
            raise requests.exceptions.HTTPError("404 Client Error")
        path.write_text(self.files[url])


def serve_hushfacebook(monkeypatch, repo_list: str | None = REAL_LIST) -> Network:
    files = {
        DOWNLOAD + "patches-0.7.1.cdx.json": SBOM,
        DOWNLOAD + "patches-0.7.1.mpp": "mpp",
        DOWNLOAD + "patches-0.7.1.tooling.json": TOOLING,
        DOWNLOAD + "release-receipt-0.7.1.json": RECEIPT,
    }
    if repo_list is not None:
        files[RAW + "patches-list.json"] = repo_list
    return serve(monkeypatch, HUSHFACEBOOK_ASSETS, files)


def serve(
    monkeypatch, assets: list[tuple[str, int]], files: dict[str, str], tag: str = TAG
) -> Network:
    network = Network(files)
    release = Release(assets, tag)
    monkeypatch.setattr(patcher.requests, "get", lambda *a, **k: release)
    monkeypatch.setattr(patcher, "download_with_progress", network.download)
    return network


@pytest.fixture
def paths(data_dir, monkeypatch) -> dict[str, Path]:
    monkeypatch.setattr(patcher, "VERSIONS_FILE", data_dir / "bin" / "versions.json")
    source_paths = patcher.get_source_paths(REPO)
    source_paths[".mpp"].parent.mkdir(parents=True, exist_ok=True)
    return source_paths


def names_loaded() -> list[object]:
    return [patch["name"] for patch in patcher.load_patch_list(REPO)]


# --- the failing shape: a release with only non-list .json assets ------------


def test_first_sync_of_a_hushfacebook_release_gets_the_real_patch_list(
    paths, monkeypatch
):
    network = serve_hushfacebook(monkeypatch)

    changed = patcher.check_and_download_release(REPO, paths, force=True)

    assert changed is True
    assert names_loaded() == ["Hide ads", "Hide reels"]
    assert DOWNLOAD + "patches-0.7.1.tooling.json" not in network.urls
    assert patcher.load_local_versions()[REPO] == TAG
    assert list(paths[".json"].parent.glob("*.candidate")) == []


def test_repair_of_a_source_whose_stored_list_is_the_sbom_heals_it(paths, monkeypatch):
    paths[".mpp"].write_text("mpp")
    paths[".json"].write_text(SBOM)
    patcher.save_local_versions({REPO: TAG})
    network = serve_hushfacebook(monkeypatch)

    changed = patcher.check_and_download_release(REPO, paths, force=False)

    assert changed is True
    assert names_loaded() == ["Hide ads", "Hide reels"]
    assert DOWNLOAD + "patches-0.7.1.mpp" not in network.urls
    assert DOWNLOAD + "patches-0.7.1.tooling.json" not in network.urls


def test_new_release_replaces_a_stored_sbom_with_the_real_list(paths, monkeypatch):
    paths[".mpp"].write_text("old mpp")
    paths[".json"].write_text(SBOM)
    patcher.save_local_versions({REPO: "v0.7.0"})
    serve_hushfacebook(monkeypatch)

    changed = patcher.check_and_download_release(REPO, paths, force=True)

    assert changed is True
    assert names_loaded() == ["Hide ads", "Hide reels"]
    assert patcher.load_local_versions()[REPO] == TAG


# --- shapes that must keep working -------------------------------------------


def test_a_release_asset_that_is_a_real_patch_list_is_used(paths, monkeypatch):
    assets = [("patches-0.7.1.mpp", 2_000_000), ("patches-list.json", 4_000)]
    network = serve(
        monkeypatch,
        assets,
        {
            DOWNLOAD + "patches-0.7.1.mpp": "mpp",
            DOWNLOAD + "patches-list.json": REAL_LIST,
            RAW + "patches-list.json": OTHER_LIST,
        },
    )

    changed = patcher.check_and_download_release(REPO, paths, force=True)

    assert changed is True
    assert names_loaded() == ["Hide ads", "Hide reels"]
    assert not any(url.startswith(RAW) for url in network.urls)


def test_base_source_shape_without_a_json_asset_uses_the_repo_file(paths, monkeypatch):
    network = serve(
        monkeypatch,
        BASE_ASSETS,
        {DOWNLOAD + "patches-1.0.mpp": "mpp", RAW + "patches-list.json": REAL_LIST},
    )

    changed = patcher.check_and_download_release(REPO, paths, force=True)

    assert changed is True
    assert names_loaded() == ["Hide ads", "Hide reels"]
    assert network.urls == [DOWNLOAD + "patches-1.0.mpp", RAW + "patches-list.json"]


def test_repo_fallback_tries_patches_json_when_patches_list_json_is_missing(
    paths, monkeypatch
):
    serve(
        monkeypatch,
        BASE_ASSETS,
        {DOWNLOAD + "patches-1.0.mpp": "mpp", RAW + "patches.json": REAL_LIST},
    )

    assert patcher.check_and_download_release(REPO, paths, force=True) is True
    assert names_loaded() == ["Hide ads", "Hide reels"]


def test_best_named_asset_wins_when_several_are_patch_lists(paths, monkeypatch):
    assets = [("patches-0.7.1.json", 900), ("patches-list.json", 900)]
    network = serve(
        monkeypatch,
        assets + BASE_ASSETS,
        {
            DOWNLOAD + "patches-0.7.1.json": OTHER_LIST,
            DOWNLOAD + "patches-list.json": REAL_LIST,
            DOWNLOAD + "patches-1.0.mpp": "mpp",
        },
    )

    patcher.check_and_download_release(REPO, paths, force=True)

    assert names_loaded() == ["Hide ads", "Hide reels"]
    assert DOWNLOAD + "patches-0.7.1.json" not in network.urls


def test_a_small_oddly_named_list_asset_is_used_when_the_repo_has_none(
    paths, monkeypatch
):
    assets = BASE_ASSETS + [("morphe-list.json", 3_000)]
    serve(
        monkeypatch,
        assets,
        {DOWNLOAD + "patches-1.0.mpp": "mpp", DOWNLOAD + "morphe-list.json": REAL_LIST},
    )

    assert patcher.check_and_download_release(REPO, paths, force=True) is True
    assert names_loaded() == ["Hide ads", "Hide reels"]


# --- an unreadable candidate never replaces a readable list ------------------


def test_unreadable_candidates_never_replace_a_readable_stored_list(
    paths, monkeypatch, capsys
):
    paths[".mpp"].write_text("old mpp")
    paths[".json"].write_text(OTHER_LIST)
    patcher.save_local_versions({REPO: "v0.7.0"})
    serve_hushfacebook(monkeypatch, repo_list=SBOM)

    patcher.check_and_download_release(REPO, paths, force=True)

    assert paths[".json"].read_text() == OTHER_LIST
    assert "No readable patch list" in capsys.readouterr().out
    assert list(paths[".json"].parent.glob("*.candidate")) == []


def test_unreadable_repo_file_never_replaces_a_readable_list_without_releases(
    paths, monkeypatch
):
    class NoReleases:
        status_code = 404
        headers: ClassVar[dict[str, str]] = {}

    paths[".json"].write_text(OTHER_LIST)
    network = Network(
        {f"https://raw.githubusercontent.com/{REPO}/main/patches-list.json": SBOM}
    )
    monkeypatch.setattr(patcher.requests, "get", lambda *a, **k: NoReleases())
    monkeypatch.setattr(patcher, "download_with_progress", network.download)

    changed = patcher.check_and_download_release(REPO, paths, force=True)

    assert changed is False
    assert paths[".json"].read_text() == OTHER_LIST


def test_source_stays_reported_when_no_candidate_is_a_list(paths, monkeypatch, capsys):
    paths[".mpp"].write_text("mpp")
    paths[".json"].write_text(SBOM)
    patcher.save_local_versions({REPO: TAG})
    serve_hushfacebook(monkeypatch, repo_list=None)

    changed = patcher.check_and_download_release(REPO, paths, force=False)

    assert changed is False
    assert paths[".json"].read_text() == SBOM
    assert "still unreadable" in capsys.readouterr().out


def test_first_sync_with_no_list_anywhere_is_not_marked_as_synced(paths, monkeypatch):
    serve_hushfacebook(monkeypatch, repo_list=None)

    changed = patcher.check_and_download_release(REPO, paths, force=True)

    assert changed is False
    assert not paths[".json"].exists()
    assert REPO not in patcher.load_local_versions()
