"""Review follow-ups for unreadable patch lists: shapes, remedy text, poll noise."""

import json
import logging
import time
from typing import ClassVar

import pytest
from fastapi import HTTPException

from src import main, patcher
from tests.conftest import make_config
from tests.test_patch_list_errors import (
    CORRUPT,
    REPO,
    VALID,
    ForbiddenScraper,
    InlineWorker,
    write_list,
)

BAD_NAMES = [["x"], {"a": 1}, 5]


def named(name: object) -> str:
    return json.dumps({"patches": [{"name": name, "compatiblePackages": {}}]})


def use_inline_worker(monkeypatch) -> None:
    monkeypatch.setattr(main, "SCRAPER_WORKER", InlineWorker())
    monkeypatch.setattr(main, "UPDATE_CACHE", {"timestamp": 0, "results": None})
    monkeypatch.setattr(main, "SCRAPER_ACTIVE", False)
    monkeypatch.setattr(main, "BUILD_STATUS", {"in_progress": False})
    monkeypatch.setattr(patcher, "get_patch_versions", lambda cfg: {})


# --- a patch name that is not text -------------------------------------------


@pytest.mark.parametrize("name", BAD_NAMES)
def test_updates_route_survives_a_patch_name_that_is_not_text(
    data_dir, monkeypatch, name
):
    write_list(named(name))
    use_inline_worker(monkeypatch)

    results = main.check_updates()

    assert REPO in results["com.a"]["error"]
    assert results["com.a"]["status"] == "Error"


@pytest.mark.parametrize("name", BAD_NAMES)
def test_version_selection_reports_a_patch_name_that_is_not_text(data_dir, name):
    write_list(named(name))

    with pytest.raises(patcher.PatchListError, match=REPO):
        patcher.get_supported_versions(
            "com.a", make_config(), scraper=ForbiddenScraper(), quiet=True
        )


@pytest.mark.parametrize("name", BAD_NAMES)
def test_dialog_route_reports_a_patch_name_that_is_not_text(data_dir, name):
    write_list(named(name))

    with pytest.raises(HTTPException) as caught:
        main.get_patches_for_app("com.a")

    assert caught.value.status_code == 502


def test_missing_or_null_patch_name_is_still_accepted(data_dir):
    write_list(json.dumps({"patches": [{"compatiblePackages": {}}, {"name": None}]}))

    assert len(patcher.load_patch_list(REPO)) == 2


# --- the remedy named in the message must be true ----------------------------


class Release:
    """Minimal GitHub release lookup response."""

    status_code = 200
    headers: ClassVar[dict[str, str]] = {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "tag_name": "v1",
            "assets": [
                {"name": "p.mpp", "browser_download_url": "https://x/p.mpp"},
                {"name": "p.json", "browser_download_url": "https://x/p.json"},
            ],
        }


@pytest.fixture
def synced_source(data_dir, monkeypatch):
    """A source at tag v1 whose JSON on disk is corrupt; downloads are recorded."""
    monkeypatch.setattr(patcher, "VERSIONS_FILE", data_dir / "bin" / "versions.json")
    paths = patcher.get_source_paths(REPO)
    paths[".mpp"].parent.mkdir(parents=True, exist_ok=True)
    paths[".mpp"].write_text("mpp")
    write_list(CORRUPT["truncated"])
    patcher.save_local_versions({REPO: "v1"})
    downloads: list[str] = []

    def fake_download(url: str, path) -> None:
        downloads.append(url)
        path.write_text(json.dumps(VALID))

    monkeypatch.setattr(patcher.requests, "get", lambda *a, **k: Release())
    monkeypatch.setattr(patcher, "download_with_progress", fake_download)
    return paths, downloads


def test_sync_at_the_same_tag_repairs_a_corrupt_list(synced_source):
    paths, downloads = synced_source

    changed = patcher.check_and_download_release(REPO, paths, force=True)

    assert changed is True
    assert downloads == ["https://x/p.json"]
    assert patcher.load_patch_list(REPO) == VALID["patches"]


def test_unthrottled_repair_does_not_wait_for_the_throttle(synced_source):
    paths, downloads = synced_source
    patcher.save_local_versions({REPO: "v1", f"{REPO}#last_check": time.time()})

    changed = patcher.check_and_download_release(REPO, paths, force=False)

    assert changed is True
    assert downloads == ["https://x/p.json"]


def test_healthy_source_stays_throttled_and_makes_no_network_call(
    synced_source, monkeypatch
):
    paths, downloads = synced_source
    paths[".json"].write_text(json.dumps(VALID))
    patcher.save_local_versions({REPO: "v1", f"{REPO}#last_check": time.time()})
    calls: list[str] = []
    monkeypatch.setattr(
        patcher.requests, "get", lambda *a, **k: calls.append("get") or Release()
    )

    changed = patcher.check_and_download_release(REPO, paths, force=False)

    assert changed is False
    assert calls == []
    assert downloads == []


def test_healthy_source_at_the_same_tag_makes_one_call_and_no_download(
    synced_source, monkeypatch
):
    paths, downloads = synced_source
    paths[".json"].write_text(json.dumps(VALID))
    calls: list[str] = []
    monkeypatch.setattr(
        patcher.requests, "get", lambda *a, **k: calls.append("get") or Release()
    )

    changed = patcher.check_and_download_release(REPO, paths, force=True)

    assert changed is False
    assert calls == ["get"]
    assert downloads == []


def test_list_still_unreadable_after_download_is_bounded_and_reported(
    synced_source, monkeypatch, capsys
):
    paths, downloads = synced_source

    def broken_download(url: str, path) -> None:
        downloads.append(url)
        path.write_text(CORRUPT["truncated"])

    monkeypatch.setattr(patcher, "download_with_progress", broken_download)

    changed = patcher.check_and_download_release(REPO, paths, force=True)

    assert changed is False
    assert downloads == ["https://x/p.json"]
    assert "still unreadable" in capsys.readouterr().out
    assert patcher.load_local_versions() == {REPO: "v1"}
    with pytest.raises(patcher.PatchListError):
        patcher.load_patch_list(REPO)


def test_sync_does_not_report_an_update_for_a_list_that_stays_unreadable(
    synced_source, monkeypatch
):
    monkeypatch.setattr(
        patcher,
        "download_with_progress",
        lambda url, path: path.write_text(CORRUPT["truncated"]),
    )
    sync_one = patcher.check_and_download_release
    monkeypatch.setattr(
        patcher,
        "check_and_download_release",
        lambda repo, wanted, force=False: repo == REPO and sync_one(repo, wanted),
    )
    config = {
        "repositories": {"cli": "tool/cli"},
        "tools": {"cli_jar": "bin/cli.jar"},
        "sources": [{"name": "Source", "repo": REPO, "active": True}],
    }

    updated = patcher.update_tools(config, quiet=True)

    assert updated is False


def test_new_release_with_a_broken_list_still_counts_as_an_update(
    synced_source, monkeypatch, capsys
):
    paths, downloads = synced_source
    patcher.save_local_versions({REPO: "v0"})

    def broken_download(url: str, path) -> None:
        downloads.append(url)
        path.write_text(CORRUPT["truncated"])

    monkeypatch.setattr(patcher, "download_with_progress", broken_download)

    changed = patcher.check_and_download_release(REPO, paths, force=True)

    assert changed is True
    assert downloads == ["https://x/p.mpp", "https://x/p.json"]
    assert "still unreadable" in capsys.readouterr().out
    assert patcher.load_local_versions()[REPO] == "v1"


class NoReleases:
    """GitHub answer for a repository that has no releases."""

    status_code = 404
    headers: ClassVar[dict[str, str]] = {}


def fallback_writing(content: str):
    """Build a stand-in for the raw-file fallback that writes `content`."""

    def fake_fallback(repo: str, branch: str, path) -> bool:
        path.write_text(content)
        return True

    return fake_fallback


def test_source_without_releases_is_repaired_from_the_raw_file(
    synced_source, monkeypatch
):
    paths, _ = synced_source
    monkeypatch.setattr(patcher.requests, "get", lambda *a, **k: NoReleases())
    monkeypatch.setattr(
        patcher, "_try_download_json_candidates", fallback_writing(json.dumps(VALID))
    )

    changed = patcher.check_and_download_release(REPO, paths, force=False)

    assert changed is True
    assert patcher.load_patch_list(REPO) == VALID["patches"]


def test_source_without_releases_whose_list_stays_unreadable_is_reported(
    synced_source, monkeypatch, capsys
):
    paths, _ = synced_source
    monkeypatch.setattr(patcher.requests, "get", lambda *a, **k: NoReleases())
    monkeypatch.setattr(
        patcher,
        "_try_download_json_candidates",
        fallback_writing(CORRUPT["truncated"]),
    )

    changed = patcher.check_and_download_release(REPO, paths, force=False)

    assert changed is False
    assert "still unreadable" in capsys.readouterr().out
    assert patcher.load_local_versions() == {REPO: "v1"}


def test_sync_downloads_the_list_again_once_the_file_is_deleted(synced_source):
    paths, downloads = synced_source
    paths[".json"].unlink()

    changed = patcher.check_and_download_release(REPO, paths, force=False)

    assert changed is True
    assert "https://x/p.json" in downloads
    assert patcher.load_patch_list(REPO) == VALID["patches"]


def test_message_names_the_file_and_does_not_promise_a_re_add_repair(data_dir):
    write_list(CORRUPT["truncated"])

    with pytest.raises(patcher.PatchListError) as caught:
        patcher.load_patch_list(REPO)

    message = str(caught.value)
    assert REPO in message
    assert "patches-own-src.json" in message
    assert "add it again" not in message


# --- polling must not flood the log ------------------------------------------


def test_updates_poll_does_not_log_the_same_error_on_every_poll(
    data_dir, monkeypatch, caplog
):
    write_list(CORRUPT["truncated"])
    use_inline_worker(monkeypatch)

    with caplog.at_level(logging.ERROR):
        results = main.check_updates()

    assert REPO in results["com.a"]["error"]
    assert caplog.records == []


# --- pinned app and current blast radius -------------------------------------


def test_updates_route_for_a_pinned_app_ignores_a_corrupt_list(data_dir, monkeypatch):
    write_list(CORRUPT["truncated"])
    use_inline_worker(monkeypatch)

    def pin(config: dict) -> None:
        config["apps"][0]["version"] = "7.7"

    main.update_config(pin)

    results = main.check_updates()

    assert results["com.a"]["latest"] == "7.7"
    assert results["com.a"]["error"] is None
    assert REPO in results["com.b"]["error"]


def test_current_behaviour_one_corrupt_list_fails_apps_that_do_not_use_it(data_dir):
    """Pins today's rule so a later narrowing is a visible decision.

    com.b selects nothing from `own/src` and has no patch there, yet an unreadable
    list in any active source still stops its version selection and the dialog.
    """
    write_list(CORRUPT["truncated"])

    with pytest.raises(patcher.PatchListError, match=REPO):
        patcher.get_supported_versions(
            "com.b", make_config(), scraper=ForbiddenScraper(), quiet=True
        )
    with pytest.raises(HTTPException) as caught:
        main.get_patches_for_app("com.b")
    assert caught.value.status_code == 502


# --- the dialog loaders ------------------------------------------------------


def test_dialogs_check_the_response_before_parsing_its_body():
    template = main.templates.get_template("index.html")
    html = template.render(
        config=make_config(), available_apps=[], metadata={}, request=None
    )

    for opener in ("async openAddModal(", "async openEditModal("):
        start = html.index(opener)
        body = html[start : html.index("async ", start + 10)]
        parse = body.index("const data = await response.json()")
        assert body.index("response.ok") < parse
        error_branch = body[body.index("response.ok") : body.index("const data =")]
        assert "response.json().catch(" in error_branch
