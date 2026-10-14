"""Shared fixtures for the pipeline and config tests."""

import socket
import threading
from pathlib import Path

import pytest
import yaml
from starlette.staticfiles import StaticFiles

from src import apkmirror_downloader, config_store, main, patcher, secrets_store

APP_A = {
    "id": "com.a",
    "name": "A",
    "include_patches": ["one"],
    "exclude_patches": [],
}
APP_B = {"id": "com.b", "name": "B", "include_patches": [], "exclude_patches": []}


def pytest_configure(config: pytest.Config) -> None:
    """Register the marker that opts a test out of the sync-scheduling stub."""
    config.addinivalue_line(
        "markers",
        "real_sync_scheduling: use the real post-token-change sync scheduling",
    )


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
    monkeypatch.setattr(main, "LOG_PATH", tmp_path / "bin" / "build.log")
    # Claims taken by a test that fakes the task must not leak into the next test.
    monkeypatch.setattr(main, "TOOLS_UPDATING", False)
    monkeypatch.setattr(main, "_ACTIVITY_COUNT", 0)
    # Values the secret store remembered for masking belong to one test only.
    for name, empty in (
        ("_current", {}),
        ("_retired", ()),
        ("_known", ()),
        ("_captured_environment", {}),
        ("_apprise_mask_current", ()),
        ("_apprise_mask_retired", ()),
        ("_apprise_mask_known", ()),
        ("_active_apprise_masks", {}),
        ("_active_apprise_mask_known", ()),
        ("_temporary_redaction", threading.local()),
    ):
        monkeypatch.setattr(secrets_store, name, empty, raising=False)
    # Both paths are bound at import, so DATA_DIR alone does not move them.
    for name in ("VERSIONS_FILE", "VERSION_CACHE_FILE"):
        real_path = getattr(patcher, name)
        monkeypatch.setattr(patcher, name, tmp_path / "bin" / real_path.name)
    icons_dir = tmp_path / "apks" / "icons"
    icons_mount = next(r for r in main.app.routes if getattr(r, "name", "") == "icons")
    monkeypatch.setattr(
        icons_mount, "app", StaticFiles(directory=str(icons_dir), check_dir=False)
    )
    # The test client sends Host: testserver; production rules stay unchanged.
    monkeypatch.setenv("ALLOWED_HOSTS", "testserver")
    for secret_name in (
        "GITHUB_TOKEN",
        "APPRISE_URLS",
    ):
        monkeypatch.delenv(secret_name, raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def no_real_browser_or_network(monkeypatch):
    """Fail any test that launches the real browser or opens a network socket.

    The attempt raises inside the code under test (which may swallow it), and
    the fixture fails the test at teardown, so it cannot pass by accident. A
    test that triggers the guard on purpose empties the yielded list.
    """
    attempts: list[str] = []

    def blocked_launch(*args, **kwargs):
        attempts.append("browser launch")
        raise RuntimeError("tests must not launch a real browser")

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def is_local_ipc(sock, address) -> bool:
        return sock.family == socket.AF_UNIX

    def blocked_connect(self, address):
        if is_local_ipc(self, address):
            return real_connect(self, address)
        attempts.append(f"socket connect {address!r}")
        raise OSError("tests must not open network connections")

    def blocked_connect_ex(self, address):
        if is_local_ipc(self, address):
            return real_connect_ex(self, address)
        attempts.append(f"socket connect_ex {address!r}")
        raise OSError("tests must not open network connections")

    monkeypatch.setattr(apkmirror_downloader, "ShardX", blocked_launch)
    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_connect_ex)
    yield attempts
    assert not attempts, f"real browser or network use in a test: {attempts}"


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


@pytest.fixture(autouse=True)
def no_token_sync_scheduling(request, monkeypatch):
    """Keep a token change over HTTP from scheduling a real tool sync.

    The routes claim and schedule a sync after the GitHub token changes; without
    this stub any test that PUTs or DELETEs a token would start one. Tests that
    exercise the scheduling carry `@pytest.mark.real_sync_scheduling`.
    """
    if request.node.get_closest_marker("real_sync_scheduling") is None:
        monkeypatch.setattr(main, "_schedule_tools_sync", lambda config: None)
