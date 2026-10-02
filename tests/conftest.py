"""Shared fixtures for the pipeline and config tests."""

from pathlib import Path

import pytest
import yaml

from src import config_store, main, patcher

APP_A = {
    "id": "com.a",
    "name": "A",
    "include_patches": ["one"],
    "exclude_patches": [],
}
APP_B = {"id": "com.b", "name": "B", "include_patches": [], "exclude_patches": []}


def make_config() -> dict:
    return {
        "repositories": {"cli": "x/cli", "apkeditor": "x/edit"},
        "sources": [{"name": "S", "repo": "own/src", "active": True}],
        "tools": {"cli_jar": "bin/cli.jar", "apkeditor_jar": "bin/edit.jar"},
        "apps": [dict(APP_A), dict(APP_B)],
        "settings": {"server_url": "http://x", "cron_schedule": "0 * * * *"},
    }


def read_config(data_dir: Path) -> dict:
    return yaml.safe_load((data_dir / "config.yaml").read_text())


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Keep every test out of the real data directory."""
    for module in (config_store, patcher, main):
        monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def data_dir(isolated_data_dir, monkeypatch):
    tmp_path = isolated_data_dir
    monkeypatch.setattr(config_store, "PROJECT_ROOT", tmp_path / "root")
    (tmp_path / "config.yaml").write_text(yaml.dump(make_config(), sort_keys=False))
    return tmp_path


@pytest.fixture
def pipeline(data_dir, monkeypatch):
    """Stub every slow step; `during_patch` hooks run in the middle of a build."""
    hooks: list = []

    def fake_patch(app, tools, raw, out, config):
        for hook in hooks:
            hook()
        Path(out).write_text("apk")
        return True, ["own/src"]

    monkeypatch.setattr(patcher, "update_tools", lambda *a, **k: False)
    monkeypatch.setattr(patcher, "get_patch_versions", lambda cfg: {"own/src": "v1"})
    monkeypatch.setattr(
        patcher, "get_supported_versions", lambda *a, **k: (["1.0"], None, None)
    )
    monkeypatch.setattr(patcher, "download_apk", lambda *a, **k: None)
    monkeypatch.setattr(patcher, "patch_app", fake_patch)
    return hooks


def run_build(package_ids: list[str]) -> None:
    patcher.run_pipeline(package_ids, quiet=True, scraper=object())


@pytest.fixture
def patch_calls(pipeline, monkeypatch):
    """Like `pipeline`, but the fake patcher writes its output and counts calls."""
    calls: list[str] = []

    def fake_patch(app, tools, raw, out, config):
        calls.append(app["id"])
        Path(out).write_text("apk")
        return True, ["own/src"]

    monkeypatch.setattr(patcher, "patch_app", fake_patch)
    return calls


def edit_app(app_id: str, **fields: object) -> None:
    def apply(config: dict) -> None:
        next(a for a in config["apps"] if a["id"] == app_id).update(fields)

    config_store.update_config(apply)
