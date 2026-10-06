"""run_pipeline with the real patch_app and a faked morphe-cli."""

import json
from pathlib import Path
from types import SimpleNamespace

from src import patcher
from tests.conftest import read_config, run_build


def install_fakes(monkeypatch, data_dir: Path, cli_lines: list[str]) -> list[list[str]]:
    """Stub everything slow except patch_app; return the CLI commands seen."""
    commands: list[list[str]] = []

    class FakePopen:
        def __init__(self, command, **kwargs):
            commands.append(command)
            Path(command[command.index("-o") + 1]).write_text("apk")
            self.lines = iter(f"{line}\n" for line in cli_lines)
            self.stdout = SimpleNamespace(readline=lambda: next(self.lines, ""))
            self.returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(patcher, "DATA_DIR", data_dir)
    monkeypatch.setattr(patcher.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        patcher.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout='package="com.a"'),
    )
    monkeypatch.setattr(patcher, "update_tools", lambda *a, **k: False)
    monkeypatch.setattr(patcher, "get_patch_versions", lambda cfg: {"own/src": "v1"})
    monkeypatch.setattr(
        patcher, "get_supported_versions", lambda *a, **k: (["1.0"], None, None)
    )
    monkeypatch.setattr(patcher, "download_apk", lambda *a, **k: None)
    return commands


def write_source(patches_json: str | None = None) -> None:
    """Write the own/src bundle with one patch named "one" for com.a."""
    paths = patcher.get_source_paths("own/src")
    paths[".mpp"].parent.mkdir(parents=True, exist_ok=True)
    paths[".mpp"].write_bytes(b"")
    paths[".json"].write_text(
        patches_json
        or json.dumps(
            {"patches": [{"name": "one", "compatiblePackages": {"com.a": None}}]}
        )
    )


def get_app(data_dir: Path, app_id: str = "com.a") -> dict:
    return next(a for a in read_config(data_dir)["apps"] if a["id"] == app_id)


def test_real_patch_app_records_layout_and_second_run_is_skipped(data_dir, monkeypatch):
    commands = install_fakes(monkeypatch, data_dir, ["Applied: one"])
    write_source()

    run_build(["com.a"])
    first = get_app(data_dir)
    run_build(["com.a"])

    assert first["status"] == "Success"
    assert first["last_successful_build"]["build_layout"] == patcher.BUILD_LAYOUT
    assert len(commands) == 1
    assert get_app(data_dir)["last_successful_build"] == first["last_successful_build"]


def test_record_without_layout_is_rebuilt_once(data_dir, monkeypatch):
    commands = install_fakes(monkeypatch, data_dir, ["Applied: one"])
    write_source()
    run_build(["com.a"])
    record = get_app(data_dir)["last_successful_build"]
    del record["build_layout"]

    from src import config_store

    config_store.update_config(
        lambda c: next(a for a in c["apps"] if a["id"] == "com.a").update(
            last_successful_build=record
        )
    )
    run_build(["com.a"])
    run_build(["com.a"])

    assert len(commands) == 2
    assert get_app(data_dir)["last_successful_build"]["build_layout"] == (
        patcher.BUILD_LAYOUT
    )


def test_zero_applied_fails_the_app_and_keeps_old_apk(data_dir, monkeypatch):
    install_fakes(monkeypatch, data_dir, ["Applying 0 patches..."])
    write_source()
    old = data_dir / "apks" / "patched" / "com.a-0.9-old.apk"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("old")

    run_build(["com.a"])

    app = get_app(data_dir)
    assert app["status"] == "Failed"
    assert "A: no patch was applied" in app["error"]
    assert "Applying 0 patches" in app["error"]
    assert old.read_text() == "old"


def test_unreadable_patch_list_shows_true_cause_and_keeps_old_apk(
    data_dir, monkeypatch
):
    install_fakes(monkeypatch, data_dir, ["Applied: one"])
    write_source('{"patches": null}')
    old = data_dir / "apks" / "patched" / "com.a-0.9-old.apk"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("old")

    run_build(["com.a"])

    app = get_app(data_dir)
    assert app["status"] in {"Error", "Failed"}
    assert "own/src" in app["error"]
    assert "unreadable" in app["error"]
    assert old.read_text() == "old"
