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
    assert app["error"] == "Selected patches were not applied"
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


def test_hush_build_patches_the_same_private_snapshot_it_validated(
    data_dir, monkeypatch
):
    from src import config_store

    patcher.DATA_DIR = data_dir
    hush_paths = patcher.get_source_paths("SysAdminDoc/HushFacebook")
    hush_paths[".mpp"].parent.mkdir(parents=True, exist_ok=True)
    hush_paths[".mpp"].write_bytes(b"bundle")
    hush_paths[".json"].write_text(
        json.dumps(
            {
                "patches": [
                    {
                        "name": "Hide ads",
                        "compatiblePackages": {"com.facebook.katana": []},
                    }
                ]
            }
        )
    )

    def configure_hush(config):
        config["sources"] = [{"repo": "SysAdminDoc/HushFacebook", "active": True}]
        config["apps"] = [
            {
                "id": "com.facebook.katana",
                "name": "Facebook",
                "include_patches": {"SysAdminDoc/HushFacebook": ["Hide ads"]},
            }
        ]

    config_store.update_config(configure_hush)
    raw_path = data_dir / "apks" / "raw" / "com.facebook.katana-1.0.apk"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(b"sdk30 original")
    seen: list[tuple[bytes, Path]] = []

    def inspect(_java, path):
        artifact = Path(path)
        contents = artifact.read_bytes()
        if contents == b"sdk30 original":
            raw_path.write_bytes(b"sdk28 concurrent upload")
        return 30 if contents == b"sdk30 original" else 28

    def patch(app, tools, raw, out, config):
        seen.append((Path(raw).read_bytes(), Path(raw)))
        Path(out).write_bytes(b"patched")
        return True, ["SysAdminDoc/HushFacebook"]

    monkeypatch.setattr(patcher, "_read_minimum_sdk", inspect)
    monkeypatch.setattr(patcher, "patch_app", patch)
    monkeypatch.setattr(patcher, "update_tools", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        patcher,
        "get_patch_versions",
        lambda _config: {"SysAdminDoc/HushFacebook": "v1"},
    )
    monkeypatch.setattr(
        patcher,
        "get_supported_versions",
        lambda *_args, **_kwargs: (["1.0"], None, None),
    )

    patcher.run_pipeline(["com.facebook.katana"], quiet=True, scraper=object())

    assert len(seen) == 1
    assert seen[0][0] == b"sdk30 original"
    assert seen[0][1] != raw_path
    assert not seen[0][1].exists()
    assert raw_path.read_bytes() == b"sdk28 concurrent upload"


def test_uninspectable_cached_hush_apk_fails_without_redownload_or_deletion(
    data_dir, monkeypatch
):
    from src import config_store

    hush_paths = patcher.get_source_paths("SysAdminDoc/HushFacebook")
    hush_paths[".mpp"].parent.mkdir(parents=True, exist_ok=True)
    hush_paths[".mpp"].write_bytes(b"bundle")
    hush_paths[".json"].write_text(
        json.dumps(
            {
                "patches": [
                    {
                        "name": "Hide ads",
                        "compatiblePackages": {"com.facebook.katana": []},
                    }
                ]
            }
        )
    )
    config_store.update_config(
        lambda config: (
            config.update(
                sources=[{"repo": "SysAdminDoc/HushFacebook", "active": True}]
            ),
            config.update(
                apps=[
                    {
                        "id": "com.facebook.katana",
                        "name": "Facebook",
                        "include_patches": {"SysAdminDoc/HushFacebook": ["Hide ads"]},
                    }
                ]
            ),
        )
    )
    raw_path = data_dir / "apks" / "raw" / "com.facebook.katana-1.0.apk"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(b"cached APK")
    old_patched = data_dir / "apks" / "patched" / "com.facebook.katana-old.apk"
    old_patched.parent.mkdir(parents=True, exist_ok=True)
    old_patched.write_bytes(b"previous patched APK")
    downloads = []

    def fail_inspection(*_args):
        raise patcher.AndroidVariantInspectionError("inspector unavailable")

    monkeypatch.setattr(patcher, "_read_minimum_sdk", fail_inspection)
    monkeypatch.setattr(
        patcher, "download_apk", lambda *_args, **_kwargs: downloads.append(True)
    )
    monkeypatch.setattr(patcher, "update_tools", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        patcher,
        "get_patch_versions",
        lambda _config: {"SysAdminDoc/HushFacebook": "v1"},
    )
    monkeypatch.setattr(
        patcher,
        "get_supported_versions",
        lambda *_args, **_kwargs: (["1.0"], None, None),
    )

    patcher.run_pipeline(["com.facebook.katana"], quiet=True, scraper=object())

    assert downloads == []
    assert raw_path.read_bytes() == b"cached APK"
    assert old_patched.read_bytes() == b"previous patched APK"
    assert list((data_dir / "apks" / ".building").iterdir()) == []
