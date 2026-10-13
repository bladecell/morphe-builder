"""An unreadable per-source patch list is reported, never silently dropped."""

import json
import logging
from pathlib import Path

import pytest
from fastapi import HTTPException

from src import config_store, main, patcher
from tests.conftest import edit_app, make_config, read_config

REPO = "own/src"
VALID = {"patches": [{"name": "one", "compatiblePackages": {"com.a": ["1.0"]}}]}
CORRUPT: dict[str, str | bytes] = {
    "truncated": '{"patches": [{"name": "one", "compatiblePackages": {"com.a": ["1',
    "entry_not_an_object": json.dumps({"patches": ["text", 3]}),
    "top_level_text": json.dumps("text"),
    "patches_not_a_list": json.dumps({"patches": {"a": 1}}),
    "invalid_utf8": b"\xff\xfe\x00",
}


def write_list(content: str | bytes | None, repo: str = REPO) -> Path:
    path = patcher.get_source_paths(repo)[".json"]
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    elif content is not None:
        path.write_text(content)
    return path


class ForbiddenScraper:
    """Fails the test if version selection falls through to APKMirror."""

    def latest_version(self, package_name: str) -> str:
        raise AssertionError("fell back to APKMirror")


@pytest.fixture(params=sorted(CORRUPT))
def corrupt_list(request, data_dir) -> Path:
    return write_list(CORRUPT[request.param])


# --- get_supported_versions --------------------------------------------------


def test_corrupt_list_raises_an_error_naming_the_repo(corrupt_list):
    with pytest.raises(patcher.PatchListError, match=REPO):
        patcher.get_supported_versions(
            "com.a", make_config(), scraper=ForbiddenScraper(), quiet=True
        )


def test_unreadable_list_raises_an_error_naming_the_repo(data_dir):
    path = write_list(None)
    path.mkdir()

    with pytest.raises(patcher.PatchListError, match=REPO):
        patcher.get_supported_versions(
            "com.a", make_config(), scraper=ForbiddenScraper(), quiet=True
        )


def test_valid_list_is_still_read(data_dir):
    write_list(json.dumps(VALID))

    versions, _, _ = patcher.get_supported_versions(
        "com.a", make_config(), scraper=ForbiddenScraper(), quiet=True
    )

    assert versions == ["1.0"]


def test_pinned_version_does_not_read_the_list(data_dir):
    write_list(CORRUPT["truncated"])
    config = make_config()
    config["apps"][0]["version"] = "7.7"

    versions, _, _ = patcher.get_supported_versions("com.a", config, quiet=True)

    assert versions == ["7.7"]


def test_missing_list_is_not_an_error(data_dir):
    class Scraper:
        def latest_version(self, package_name: str) -> str:
            return "9.9"

    versions, _, _ = patcher.get_supported_versions(
        "com.a", make_config(), scraper=Scraper(), quiet=True
    )

    assert versions == ["9.9"]


# --- run_pipeline ------------------------------------------------------------


def test_build_stops_with_an_error_on_the_app_and_keeps_the_served_apk(
    data_dir, monkeypatch
):
    patched_dir = data_dir / "apks" / "patched"
    patched_dir.mkdir(parents=True)
    served = patched_dir / "com.a-1.0-v1-abcd1234.apk"
    served.write_text("old apk")
    record = {"filename": served.name, "combined_version": "1.0-v1-abcd1234"}
    config = make_config()
    config["apps"][0]["last_successful_build"] = record
    config_store.save_config(config)
    write_list(CORRUPT["truncated"])
    patch_calls: list[str] = []
    monkeypatch.setattr(patcher, "update_tools", lambda *a, **k: False)
    monkeypatch.setattr(patcher, "get_patch_versions", lambda cfg: {REPO: "v1"})
    monkeypatch.setattr(
        patcher, "patch_app", lambda app, *a, **k: patch_calls.append(app["id"])
    )

    patcher.run_pipeline(["com.a"], quiet=True, scraper=ForbiddenScraper())

    app = next(a for a in read_config(data_dir)["apps"] if a["id"] == "com.a")
    assert app["status"] == "Error"
    assert REPO in app["error"]
    assert app["last_successful_build"] == record
    assert served.read_text() == "old apk"
    assert patch_calls == []


# --- callers in main ---------------------------------------------------------


class InlineWorker:
    """Stands in for the scraper thread: runs the call in the caller."""

    def execute(self, func, *args, **kwargs):
        return func(*args, scraper=None, **kwargs)


def test_updates_route_reports_the_error_for_that_app_without_failing(
    data_dir, monkeypatch
):
    write_list(CORRUPT["truncated"])
    monkeypatch.setattr(main, "SCRAPER_WORKER", InlineWorker())
    monkeypatch.setattr(main, "UPDATE_CACHE", {"timestamp": 0, "results": None})
    monkeypatch.setattr(main, "SCRAPER_ACTIVE", False)
    monkeypatch.setattr(main, "BUILD_STATUS", {"in_progress": False})
    monkeypatch.setattr(patcher, "get_patch_versions", lambda cfg: {})

    results = main.check_updates()

    assert set(results) == {"com.a", "com.b"}
    assert REPO in results["com.a"]["error"]
    assert results["com.a"]["status"] == "Error"
    assert results["com.a"]["latest"] == "Unknown"


def test_version_refresh_logs_the_error_and_continues_with_other_apps(
    data_dir, monkeypatch, caplog
):
    refreshed: list[str] = []

    def fake_versions(package_id, config, scraper=None, force_refresh=False):
        if package_id == "com.a":
            raise patcher.PatchListError(REPO, "broken")
        refreshed.append(package_id)
        return [], None, None

    monkeypatch.setattr(main, "get_supported_versions", fake_versions)
    monkeypatch.setattr(main, "SCRAPER_WORKER", InlineWorker())
    monkeypatch.setattr(main, "SCRAPER_ACTIVE", False)
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)

    with caplog.at_level(logging.ERROR):
        main.refresh_versions_task()

    assert refreshed == ["com.b"]
    assert REPO in caplog.text


# --- the patch dialog --------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CORRUPT))
def test_dialog_route_answers_502_with_the_repo_in_the_detail(data_dir, name):
    write_list(CORRUPT[name])

    with pytest.raises(HTTPException) as caught:
        main.get_patches_for_app("com.a")

    assert caught.value.status_code == 502
    assert REPO in caught.value.detail


def test_dialog_route_still_lists_a_healthy_source(data_dir):
    write_list(json.dumps(VALID))

    patches = main.get_patches_for_app("com.a")

    assert [p["name"] for p in patches] == ["one"]


def test_dialog_checks_patch_response_before_parsing_grouped_catalog():
    template = main.templates.get_template("index.html")
    html = template.render(
        config=make_config(), available_apps=[], metadata={}, request=None
    )

    for opener in ("async openAddModal(", "async openEditModal("):
        start = html.index(opener)
        body = html[start : html.index("async ", start + 10)]
        assert "response.ok" in body
        assert body.index("response.ok") < body.index("response.json()")
        assert body.index("response.json()") < body.index("initializePatchCatalog(data")
        assert "const error = await this.responseDetail(response" in body
        assert "this.patchError = error" in body


# --- malformed entries that used to escape as a TypeError --------------------


def _prepare_updates_route(monkeypatch) -> None:
    monkeypatch.setattr(main, "SCRAPER_WORKER", InlineWorker())
    monkeypatch.setattr(main, "UPDATE_CACHE", {"timestamp": 0, "results": None})
    monkeypatch.setattr(main, "SCRAPER_ACTIVE", False)
    monkeypatch.setattr(main, "BUILD_STATUS", {"in_progress": False})
    monkeypatch.setattr(patcher, "get_patch_versions", lambda cfg: {})


@pytest.mark.parametrize("bad_name", [["a", "b"], {"k": "v"}, 3])
def test_updates_route_survives_an_entry_whose_name_is_not_a_string(
    data_dir, monkeypatch, bad_name
):
    write_list(json.dumps({"patches": [{"name": bad_name}]}))
    _prepare_updates_route(monkeypatch)

    results = main.check_updates()

    assert results["com.a"]["status"] == "Error"
    assert REPO in results["com.a"]["error"]


def test_refresh_survives_an_entry_whose_name_is_not_a_string(data_dir, monkeypatch):
    write_list(json.dumps({"patches": [{"name": ["a"]}]}))
    real_versions = main.get_supported_versions
    finished: list[str] = []

    def spy(package_id, config, **kwargs):
        try:
            return real_versions(package_id, config, **kwargs)
        finally:
            finished.append(package_id)

    monkeypatch.setattr(main, "get_supported_versions", spy)
    monkeypatch.setattr(main, "SCRAPER_WORKER", InlineWorker())
    monkeypatch.setattr(main, "SCRAPER_ACTIVE", False)
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)

    main.refresh_versions_task()

    assert finished == ["com.a", "com.b"]


def test_odd_version_entries_do_not_break_version_selection(data_dir):
    patches = [
        {"name": "one", "compatiblePackages": {"com.a": [1, ["x"], None, "2.0"]}},
        {
            "name": "two",
            "compatiblePackages": [
                {"packageName": "com.a", "targets": ["junk", {"version": "3.0"}]}
            ],
        },
    ]
    write_list(json.dumps({"patches": patches}))

    versions, _, _ = patcher.get_supported_versions(
        "com.a", make_config(), scraper=ForbiddenScraper(), quiet=True
    )

    assert len(versions) == 1


# --- the updates route with a pinned app -------------------------------------


def test_updates_route_pinned_app_ignores_a_corrupt_list(data_dir, monkeypatch):
    write_list(CORRUPT["truncated"])
    edit_app("com.a", version="7.7")
    _prepare_updates_route(monkeypatch)

    results = main.check_updates()

    assert results["com.a"]["latest"] == "7.7"
    assert results["com.a"]["error"] is None
    assert results["com.a"]["status"] != "Error"
    assert results["com.b"]["status"] == "Error"


# --- current behaviour, pinned so the decision to narrow it is visible -------


def test_current_behaviour_one_corrupt_source_blocks_apps_that_do_not_use_it(
    data_dir, monkeypatch
):
    write_list(CORRUPT["truncated"])
    monkeypatch.setattr(patcher, "update_tools", lambda *a, **k: False)
    monkeypatch.setattr(patcher, "get_patch_versions", lambda cfg: {REPO: "v1"})
    patch_calls: list[str] = []
    monkeypatch.setattr(
        patcher, "patch_app", lambda app, *a, **k: patch_calls.append(app["id"])
    )

    patcher.run_pipeline(["com.b"], quiet=True, scraper=ForbiddenScraper())

    app = next(a for a in read_config(data_dir)["apps"] if a["id"] == "com.b")
    assert app["status"] == "Error"
    assert REPO in app["error"]
    assert patch_calls == []
    with pytest.raises(HTTPException) as caught:
        main.get_patches_for_app("com.b")
    assert caught.value.status_code == 502
