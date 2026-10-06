"""Config safety: atomic writes, serialised read-modify-write, pipeline ownership."""

import re
import threading
import time

import pytest
import yaml
from fastapi import HTTPException

from src import config_store, main, patcher
from tests.conftest import (
    edit_app,
    make_config,
    read_config,
    run_build,
)

# --- atomic write -----------------------------------------------------------


def test_failed_write_keeps_previous_file(data_dir, monkeypatch):
    before = (data_dir / "config.yaml").read_text()

    def die_after_partial_write(fd):
        raise OSError("disk full")

    monkeypatch.setattr(config_store.os, "fsync", die_after_partial_write)
    with pytest.raises(OSError):
        config_store.save_config({"apps": []})
    assert (data_dir / "config.yaml").read_text() == before
    assert [p.name for p in data_dir.iterdir()] == ["config.yaml"]


def test_failed_replace_keeps_previous_file(data_dir, monkeypatch):
    before = (data_dir / "config.yaml").read_text()

    def no_replace(src, dst):
        raise OSError("killed")

    monkeypatch.setattr(config_store.os, "replace", no_replace)
    with pytest.raises(OSError):
        config_store.save_config({"apps": []})
    assert (data_dir / "config.yaml").read_text() == before
    assert [p.name for p in data_dir.iterdir()] == ["config.yaml"]


def test_reader_never_sees_partial_file(data_dir):
    big = make_config()
    big["apps"] = [
        {"id": f"com.app{i}", "name": "n" * 50, "include_patches": ["p"] * 10}
        for i in range(100)
    ]
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        while not stop.is_set():
            config_store.save_config(big)
            config_store.save_config(make_config())

    def reader() -> None:
        try:
            for _ in range(40):
                assert config_store.load_config()["repositories"]["cli"] == "x/cli"
        except (AssertionError, KeyError, TypeError, yaml.YAMLError) as exc:
            errors.append(exc)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    try:
        reader()
    finally:
        stop.set()
        writer_thread.join()
    assert errors == []


def test_save_preserves_key_order(data_dir):
    config_store.save_config(make_config())
    assert list(read_config(data_dir)) == [
        "repositories",
        "sources",
        "tools",
        "apps",
        "settings",
    ]


# --- seeding ----------------------------------------------------------------


def test_missing_config_is_generated_from_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store, "DATA_DIR", tmp_path / "fresh")
    monkeypatch.setattr(config_store, "PROJECT_ROOT", tmp_path / "root")
    config = config_store.load_config()
    assert config == config_store.DEFAULT_CONFIG
    assert (tmp_path / "fresh" / "config.yaml").exists()


def test_missing_config_is_seeded_from_project_root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.yaml").write_text("apps: []\nsettings: {a: 1}\n")
    monkeypatch.setattr(config_store, "DATA_DIR", tmp_path / "fresh")
    monkeypatch.setattr(config_store, "PROJECT_ROOT", root)
    assert config_store.load_config() == {"apps": [], "settings": {"a": 1}}
    assert (tmp_path / "fresh" / "config.yaml").read_text().startswith("apps: []")


# --- serialised read-modify-write ---------------------------------------------


def test_update_config_returns_saved_config(data_dir):
    result = config_store.update_config(
        lambda cfg: cfg["settings"].update(server_url="http://y")
    )
    assert result["settings"]["server_url"] == "http://y"
    assert read_config(data_dir) == result


def test_update_config_failure_saves_nothing(data_dir):
    before = (data_dir / "config.yaml").read_text()

    def explode(cfg: dict) -> None:
        cfg["apps"].clear()
        raise ValueError("nope")

    with pytest.raises(ValueError):
        config_store.update_config(explode)
    assert (data_dir / "config.yaml").read_text() == before


def test_concurrent_updates_lose_nothing(data_dir):
    def add(index: int) -> None:
        def mutate(cfg: dict) -> None:
            current = list(cfg["apps"])
            time.sleep(0.005)  # widen the window a lost update needs
            cfg["apps"] = current + [{"id": f"com.t{index}", "name": "t"}]

        config_store.update_config(mutate)

    threads = [threading.Thread(target=add, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ids = {a["id"] for a in read_config(data_dir)["apps"]}
    assert {f"com.t{i}" for i in range(20)} <= ids
    assert {"com.a", "com.b"} <= ids


def test_handlers_use_current_file_contents(data_dir):
    main.delete_app_from_config(main.DeleteAppPayload(package_id="com.a"))
    main.save_settings(
        main.SaveSettingsPayload(server_url="http://z/", cron_schedule="")
    )
    saved = read_config(data_dir)
    assert [a["id"] for a in saved["apps"]] == ["com.b"]
    assert saved["settings"]["server_url"] == "http://z"


# --- pipeline write ownership -------------------------------------------------


def test_pipeline_writes_result_fields(data_dir, pipeline):
    run_build(["com.a"])
    app = {a["id"]: a for a in read_config(data_dir)["apps"]}["com.a"]
    assert app["status"] == "Success"
    assert app["error"] is None
    assert app["last_successful_build"]["app_version"] == "1.0"
    assert app["include_patches"] == ["one"]


def test_ui_changes_during_build_survive(data_dir, pipeline):
    def ui_edits() -> None:
        main.toggle_source(main.AddSourcePayload(repo="own/src"))
        main.save_settings(
            main.SaveSettingsPayload(server_url="http://new", cron_schedule="")
        )
        main.add_app_to_config(
            main.AddAppPayload(package_id="com.c", name="C", include_patches=[])
        )

    pipeline.append(ui_edits)
    run_build(["com.a"])
    saved = read_config(data_dir)
    assert saved["sources"][0]["active"] is False
    assert saved["settings"]["server_url"] == "http://new"
    assert {a["id"] for a in saved["apps"]} == {"com.a", "com.b", "com.c"}
    by_id = {a["id"]: a for a in saved["apps"]}
    assert by_id["com.a"]["status"] == "Success"


def test_deleted_app_is_not_resurrected(data_dir, pipeline):
    pipeline.append(
        lambda: main.delete_app_from_config(main.DeleteAppPayload(package_id="com.a"))
    )
    run_build(["com.a"])
    assert [a["id"] for a in read_config(data_dir)["apps"]] == ["com.b"]


def test_edited_app_keeps_edit_and_gets_result(data_dir, pipeline):
    pipeline.append(
        lambda: main.add_app_to_config(
            main.AddAppPayload(
                package_id="com.a", name="Renamed", include_patches=["two"]
            )
        )
    )
    run_build(["com.a"])
    app = {a["id"]: a for a in read_config(data_dir)["apps"]}["com.a"]
    assert app["name"] == "Renamed"
    assert app["include_patches"] == ["two"]
    assert app["status"] == "Success"
    assert re.fullmatch(
        r"1\.0-v1-[0-9a-f]{8}", app["last_successful_build"]["combined_version"]
    )


def test_pipeline_error_result_is_merged(data_dir, pipeline, monkeypatch):
    monkeypatch.setattr(patcher, "get_supported_versions", lambda *a, **k: ([], 0, 0))
    pipeline.append(lambda: None)
    run_build(["com.a"])
    app = {a["id"]: a for a in read_config(data_dir)["apps"]}["com.a"]
    assert (app["status"], app["error"]) == ("Error", "No supported versions")


def test_update_tools_persists_only_versions(data_dir, monkeypatch):
    monkeypatch.setattr(
        patcher, "load_local_versions", lambda: {"x/cli": "v9", "own/src": "v3"}
    )
    monkeypatch.setattr(patcher, "check_and_download_release", lambda *a, **k: True)
    stale = make_config()
    # A UI edit lands on disk after `stale` was loaded.
    config_store.update_config(lambda cfg: cfg["apps"].pop(0))
    patcher.update_tools(stale, quiet=True)
    saved = read_config(data_dir)
    assert [a["id"] for a in saved["apps"]] == ["com.b"]
    assert saved["tools"]["cli_version"] == "v9"
    assert saved["sources"][0]["version"] == "v3"
    assert stale["tools"]["cli_version"] == "v9"


# --- add_source with a slow sync ----------------------------------------------


@pytest.fixture
def slow_sync(data_dir, monkeypatch):
    paths = {".mpp": data_dir / "p.mpp", ".json": data_dir / "p.json"}
    entered = threading.Event()
    release = threading.Event()

    def fake_sync(repo, sync_paths, force=False):
        entered.set()
        assert release.wait(5)
        for path in sync_paths.values():
            path.write_text("x")
        return True

    monkeypatch.setattr(main, "get_source_paths", lambda repo: paths)
    monkeypatch.setattr(main, "check_and_download_release", fake_sync)
    monkeypatch.setattr(main, "load_local_versions", lambda: {"new/repo": "v1"})
    return entered, release


def start_add_source(repo: str) -> tuple[threading.Thread, list]:
    outcome: list = []

    def call() -> None:
        try:
            outcome.append(main.add_source(main.AddSourcePayload(repo=repo)))
        except HTTPException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=call)
    thread.start()
    return thread, outcome


def test_add_source_does_not_hold_lock_during_sync(data_dir, slow_sync):
    entered, release = slow_sync
    thread, outcome = start_add_source("new/repo")
    try:
        assert entered.wait(5)
        started = time.monotonic()
        main.add_app_to_config(
            main.AddAppPayload(package_id="com.c", name="C", include_patches=[])
        )
        assert time.monotonic() - started < 1
    finally:
        release.set()
        thread.join(5)
    saved = read_config(data_dir)
    assert [s["repo"] for s in saved["sources"]] == ["own/src", "new/repo"]
    assert "com.c" in {a["id"] for a in saved["apps"]}
    assert outcome == [{"status": "success", "name": "new Patches"}]


def test_add_source_refuses_duplicate_that_appeared_during_sync(data_dir, slow_sync):
    entered, release = slow_sync
    thread, outcome = start_add_source("new/repo")
    try:
        assert entered.wait(5)
        config_store.update_config(
            lambda cfg: cfg["sources"].append(
                {"name": "other", "repo": "New/Repo", "active": True}
            )
        )
    finally:
        release.set()
        thread.join(5)
    assert isinstance(outcome[0], HTTPException)
    assert outcome[0].status_code == 400
    assert [s["name"] for s in read_config(data_dir)["sources"]] == ["S", "other"]


def test_source_updates_route_is_gone_so_nothing_writes_source_versions_from_a_get(
    data_dir,
):
    from fastapi.testclient import TestClient

    before = (data_dir / "config.yaml").read_bytes()

    response = TestClient(main.app).get("/api/ui/source_updates")

    assert response.status_code == 404
    assert (data_dir / "config.yaml").read_bytes() == before


# --- patch selection triggers a rebuild ---------------------------------------


def test_unchanged_selection_is_not_rebuilt(data_dir, patch_calls):
    run_build(["com.a"])
    run_build(["com.a"])

    assert patch_calls == ["com.a"]


def test_changed_selection_is_rebuilt(data_dir, patch_calls):
    run_build(["com.a"])
    edit_app("com.a", include_patches=["two", "one"])

    run_build(["com.a"])

    assert patch_calls == ["com.a", "com.a"]
    app = {a["id"]: a for a in read_config(data_dir)["apps"]}["com.a"]
    assert app["last_successful_build"]["patches"] == {
        "include": ["one", "two"],
        "exclude": [],
    }


def test_reordered_selection_is_not_rebuilt(data_dir, patch_calls):
    edit_app("com.a", include_patches=["one", "two"])
    run_build(["com.a"])
    edit_app("com.a", include_patches=["two", "one"])

    run_build(["com.a"])

    assert patch_calls == ["com.a"]


def test_build_without_recorded_selection_is_rebuilt(data_dir, patch_calls):
    run_build(["com.a"])

    def forget_selection(config: dict) -> None:
        del config["apps"][0]["last_successful_build"]["patches"]

    config_store.update_config(forget_selection)

    run_build(["com.a"])

    assert patch_calls == ["com.a", "com.a"]


def test_updates_endpoint_flags_changed_selection(data_dir, patch_calls, monkeypatch):
    def keep_only_pinned_a(config: dict) -> None:
        config["apps"] = [a for a in config["apps"] if a["id"] == "com.a"]
        config["apps"][0]["version"] = "1.0"

    config_store.update_config(keep_only_pinned_a)
    run_build(["com.a"])
    fresh_cache = {"timestamp": 0, "results": None}
    monkeypatch.setattr(main, "UPDATE_CACHE", dict(fresh_cache))
    assert main.check_updates()["com.a"]["has_update"] is False

    edit_app("com.a", include_patches=["two"])
    monkeypatch.setattr(main, "UPDATE_CACHE", dict(fresh_cache))

    assert main.check_updates()["com.a"]["has_update"] is True
