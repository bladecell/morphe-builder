import json

import pytest
from fastapi import HTTPException

from src import main, patcher


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("MorpheApp/morphe-patches", "MorpheApp/morphe-patches"),
        ("https://github.com/MorpheApp/morphe-patches", "MorpheApp/morphe-patches"),
        ("http://www.github.com/MorpheApp/morphe-patches/", "MorpheApp/morphe-patches"),
        ("github.com/MorpheApp/morphe-patches.git", "MorpheApp/morphe-patches"),
        ("https://github.com/a/b/tree/main/src", "a/b"),
        ("  a/b/releases  ", "a/b"),
    ],
)
def test_normalize_repo_accepts(raw, expected):
    assert patcher.normalize_repo(raw) == expected


@pytest.mark.parametrize("raw", ["", "justuser", "https://github.com/user", "a b/c", "a/b c"])
def test_normalize_repo_rejects(raw):
    assert patcher.normalize_repo(raw) is None


@pytest.fixture
def config_store(monkeypatch):
    store = {"sources": [{"name": "X", "repo": "Owner/Repo", "active": True}]}
    monkeypatch.setattr(main, "load_config", lambda: store)
    monkeypatch.setattr(main, "save_config", lambda cfg: None)
    monkeypatch.setattr(main, "load_local_versions", lambda: {"new/repo": "v1"})
    return store


def test_add_source_invalid(config_store):
    with pytest.raises(HTTPException) as exc:
        main.add_source(main.AddSourcePayload(repo="nonsense"))
    assert exc.value.status_code == 400


def test_add_source_duplicate_is_case_insensitive(config_store):
    with pytest.raises(HTTPException) as exc:
        main.add_source(main.AddSourcePayload(repo="https://github.com/owner/repo"))
    assert exc.value.status_code == 400


@pytest.fixture
def source_files(tmp_path, monkeypatch):
    paths = {".mpp": tmp_path / "p.mpp", ".json": tmp_path / "p.json"}
    monkeypatch.setattr(main, "get_source_paths", lambda repo: paths)
    return paths


@pytest.mark.parametrize(
    "present, expected",
    [([], "patches JSON"), ([".mpp"], "patches JSON"), ([".json"], ".mpp patch bundle")],
)
def test_add_source_reports_missing_file(config_store, source_files, monkeypatch, present, expected):
    for ext in present:
        source_files[ext].write_text("x")
    monkeypatch.setattr(main, "check_and_download_release", lambda *a, **k: False)
    with pytest.raises(HTTPException) as exc:
        main.add_source(main.AddSourcePayload(repo="new/repo"))
    assert exc.value.status_code == 502
    assert expected in exc.value.detail
    assert len(config_store["sources"]) == 1


def test_add_source_success_forces_sync(config_store, source_files, monkeypatch):
    calls = []

    def fake_sync(repo, paths, force=False):
        calls.append(force)
        for path in paths.values():
            path.write_text("x")
        return True

    monkeypatch.setattr(main, "check_and_download_release", fake_sync)
    result = main.add_source(main.AddSourcePayload(repo="github.com/new/repo.git"))
    assert result["status"] == "success"
    assert calls == [True]
    assert config_store["sources"][-1] == {
        "name": "new Patches", "repo": "new/repo", "active": True, "version": "v1",
    }


def test_add_source_already_synced_files_succeed(config_store, source_files, monkeypatch):
    for path in source_files.values():
        path.write_text("x")
    monkeypatch.setattr(main, "check_and_download_release", lambda *a, **k: False)
    assert main.add_source(main.AddSourcePayload(repo="new/repo"))["status"] == "success"


def test_discovery_uses_display_names(tmp_path, monkeypatch):
    patch_json = tmp_path / "p.json"
    patch_json.write_text(json.dumps({"patches": [
        {"name": "x", "compatiblePackages": [
            {"packageName": "com.google.android.youtube", "name": "YouTube"},
            {"packageName": "com.no.name"},
        ]},
    ]}))
    monkeypatch.setattr(patcher, "get_source_paths", lambda repo: {".json": patch_json})
    apps = patcher.get_all_available_apps({"sources": [{"repo": "a/b"}]})
    assert apps == [
        {"pkg": "com.no.name", "name": "com.no.name"},
        {"pkg": "com.google.android.youtube", "name": "YouTube"},
    ]


def test_throttle_skipped_when_forced(tmp_path, monkeypatch):
    path = tmp_path / "f.json"
    path.write_text("{}")
    monkeypatch.setattr(patcher, "load_local_versions", lambda: {"a/b": "v1", "a/b#last_check": 1e12})
    hits = []

    def fake_get(*a, **k):
        hits.append(1)
        raise patcher.requests.exceptions.RequestException("stop")

    monkeypatch.setattr(patcher.requests, "get", fake_get)
    assert patcher.check_and_download_release("a/b", {".json": path}) is False
    assert hits == []
    patcher.check_and_download_release("a/b", {".json": path}, force=True)
    assert hits == [1]


def test_update_tools_passes_force(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(patcher, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        patcher, "check_and_download_release", lambda *a, **k: seen.append(k.get("force")) or False
    )
    monkeypatch.setattr(patcher, "load_local_versions", lambda: {})
    monkeypatch.setattr(patcher, "save_config_safe", lambda *a, **k: None, raising=False)
    config = {
        "repositories": {"cli": "x/cli"},
        "tools": {"cli_jar": "cli.jar"},
        "sources": [{"name": "s", "repo": "a/b", "active": True}],
    }
    patcher.update_tools(config, quiet=True, force=True)
    assert seen == [True, True]
