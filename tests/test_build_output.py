"""What Obtainium is served: version string, filename and APK survival."""

import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi import HTTPException

from src import config_store, main, patcher
from tests.conftest import edit_app, read_config, run_build

MORPHE = "MorpheApp/morphe-patches"
THIRD = "own/src"


@dataclass
class BuildEnv:
    """Knobs and observations for the faked slow steps of a build."""

    data_dir: Path
    versions: dict[str, str] = field(
        default_factory=lambda: {MORPHE: "m1", THIRD: "t1"}
    )
    used: list[str] = field(default_factory=lambda: [THIRD])
    app_version: str = "1.0"
    tools_updated: bool = False
    mode: str = "ok"
    calls: int = 0
    during_patch: list[Callable[[Path], None]] = field(default_factory=list)
    outputs: list[Path] = field(default_factory=list)

    @property
    def patched_dir(self) -> Path:
        return self.data_dir / "apks" / "patched"

    def served(self) -> dict:
        """The Obtainium listing entry for com.a."""
        listing = main.get_apps(_FakeRequest())["apps"]
        return next(a for a in listing if a["id"] == "com.a")

    def build(self) -> None:
        run_build(["com.a"])

    def build_info(self) -> dict:
        app = {a["id"]: a for a in read_config(self.data_dir)["apps"]}["com.a"]
        return app["last_successful_build"]


class _FakeRequest:
    base_url = "http://x/"


@pytest.fixture
def env(data_dir, monkeypatch) -> BuildEnv:
    state = BuildEnv(data_dir)

    def add_sources(config: dict) -> None:
        config["sources"] = [
            {"name": "Morphe Patches", "repo": MORPHE, "active": True},
            {"name": "S", "repo": THIRD, "active": True},
        ]

    config_store.update_config(add_sources)
    edit_app("com.a", obtainium_id="morphe_a")

    def fake_patch(app, tools, raw, out, config):
        state.calls += 1
        state.outputs.append(Path(out))
        for hook in state.during_patch:
            hook(Path(out))
        if state.mode == "ok":
            Path(out).write_text(f"apk-{state.calls}")
            return True, list(state.used)
        Path(out).write_text("partial")
        if state.mode == "crash":
            raise RuntimeError("morphe-cli died")
        if state.mode == "interrupt":
            raise KeyboardInterrupt
        Path(out).unlink()
        return False, []

    monkeypatch.setattr(patcher, "update_tools", lambda *a, **k: state.tools_updated)
    monkeypatch.setattr(patcher, "get_patch_versions", lambda cfg: dict(state.versions))
    monkeypatch.setattr(
        patcher,
        "get_supported_versions",
        lambda *a, **k: ([state.app_version], None, None),
    )
    monkeypatch.setattr(patcher, "download_apk", lambda *a, **k: None)
    monkeypatch.setattr(patcher, "patch_app", fake_patch)
    return state


# --- criterion 1 and 2: the served string -----------------------------------


def test_string_shape_uses_third_party_tag_when_morphe_unused(env):
    env.build()

    assert re.fullmatch(r"1\.0-t1-[0-9a-f]{8}", env.build_info()["combined_version"])
    assert env.served()["version"] == env.build_info()["combined_version"]


def test_string_uses_morphe_tag_when_morphe_used(env):
    env.used = [THIRD, MORPHE]

    env.build()

    assert re.fullmatch(r"1\.0-m1-[0-9a-f]{8}", env.build_info()["combined_version"])


def test_filename_follows_the_string(env):
    env.build()

    info = env.build_info()
    assert info["filename"] == f"com.a-{info['combined_version']}.apk"
    assert env.served()["download_url"].endswith(f"/download/{info['filename']}")


def test_used_third_party_source_update_changes_string_and_filename(env):
    env.used = [THIRD, MORPHE]
    env.build()
    before = env.served()
    env.versions[THIRD] = "t2"

    env.build()

    after = env.served()
    assert after["version"] != before["version"]
    assert after["download_url"] != before["download_url"]
    assert after["version"].startswith("1.0-m1-")


def test_selection_change_changes_string_and_filename(env):
    env.build()
    before = env.served()
    edit_app("com.a", include_patches=["one", "two"])

    env.build()

    after = env.served()
    assert after["version"] != before["version"]
    assert after["download_url"] != before["download_url"]
    assert after["version"].startswith("1.0-t1-")


def test_app_version_change_changes_string(env):
    env.build()
    before = env.served()["version"]
    env.app_version = "1.1"

    env.build()

    assert env.served()["version"].startswith("1.1-t1-")
    assert env.served()["version"] != before


def test_unused_source_update_changes_nothing(env):
    env.build()
    before = env.served()
    env.versions[MORPHE] = "m2"

    env.build()

    assert env.calls == 1
    assert env.served() == before


def test_tool_update_rebuilds_without_changing_string(env):
    env.build()
    before = env.served()
    env.tools_updated = True

    env.build()

    assert env.calls == 2
    assert env.served() == before


def test_reordered_selection_keeps_string(env):
    edit_app("com.a", include_patches=["one", "two"])
    env.build()
    before = env.served()
    edit_app("com.a", include_patches=["two", "one"])
    env.tools_updated = True

    env.build()

    assert env.served() == before


def test_string_is_stable_across_processes():
    code = (
        "from src import patcher;"
        "print(patcher.build_combined_version("
        "'1.0', {'b/x': 'v2', 'MorpheApp/morphe-patches': 'v1'},"
        "{'include': ['p'], 'exclude': []}))"
    )
    outputs = {
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout
        for seed in ("1", "2", "3")
    }

    assert len(outputs) == 1
    assert outputs.pop().startswith("1.0-v1-")


# --- criterion 3: skipping -----------------------------------------------------


def test_unchanged_inputs_are_skipped(env):
    env.build()
    env.build()

    assert env.calls == 1


def test_missing_file_is_rebuilt(env):
    env.build()
    (env.patched_dir / env.build_info()["filename"]).unlink()

    env.build()

    assert env.calls == 2


def test_used_source_update_is_not_skipped(env):
    env.build()
    env.versions[THIRD] = "t2"

    env.build()

    assert env.calls == 2


def test_deactivated_used_source_is_rebuilt(env):
    env.build()
    del env.versions[THIRD]

    env.build()

    assert env.calls == 2


# --- criterion 4: failures and partial output ----------------------------------


@pytest.mark.parametrize("mode", ["fail", "crash", "interrupt"])
def test_failed_patch_leaves_previous_apk_served(env, mode):
    env.build()
    before = env.served()
    previous = env.patched_dir / env.build_info()["filename"]
    edit_app("com.a", include_patches=["two"])
    env.mode = mode

    if mode == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            env.build()
    else:
        env.build()

    assert previous.read_text() == "apk-1"
    assert env.served() == before
    assert main.download_app_by_name(before["download_url"].rsplit("/", 1)[1])


@pytest.mark.parametrize("mode", ["fail", "crash", "interrupt"])
def test_failed_patch_leaves_no_stray_apk(env, mode):
    env.build()
    edit_app("com.a", include_patches=["two"])
    env.mode = mode

    try:
        env.build()
    except KeyboardInterrupt:
        pass

    found = sorted(
        p.relative_to(env.data_dir / "apks").as_posix()
        for p in (env.data_dir / "apks").rglob("*")
        if p.is_file() and "raw" not in p.parts
    )
    assert found == [f"patched/{env.build_info()['filename']}"]


def test_output_is_invisible_while_patching(env):
    env.build()
    first = env.build_info()["filename"]
    seen: list[tuple[list[str], str]] = []

    def observe(out: Path) -> None:
        seen.append(
            (
                sorted(p.name for p in env.patched_dir.glob("com.a-*")),
                (env.patched_dir / first).read_text(),
            )
        )

    env.during_patch.append(observe)
    env.tools_updated = True

    env.build()

    assert seen == [([first], "apk-1")]
    assert (env.patched_dir / first).read_text() == "apk-2"
    assert env.outputs[-1].parent != env.patched_dir
    assert not env.outputs[-1].exists()


def test_old_apks_removed_only_after_new_one_is_in_place(env):
    env.build()
    old = env.build_info()["filename"]
    (env.patched_dir / "com.a-0.9-legacy.apk").write_text("legacy")
    edit_app("com.a", include_patches=["two"])
    on_disk_during: list[list[str]] = []
    env.during_patch.append(
        lambda out: on_disk_during.append(
            sorted(p.name for p in env.patched_dir.iterdir())
        )
    )

    env.build()

    assert on_disk_during == [sorted([old, "com.a-0.9-legacy.apk"])]
    assert [p.name for p in env.patched_dir.iterdir()] == [env.build_info()["filename"]]


def test_move_into_place_is_an_atomic_replace(env, monkeypatch):
    moves: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src, dst):
        moves.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(patcher.os, "replace", spy)

    env.build()

    final = str(env.patched_dir / env.build_info()["filename"])
    assert [dst for _, dst in moves if dst == final] == [final]
    assert env.outputs[0].is_relative_to(env.data_dir / "apks")


def test_stale_partial_output_from_a_killed_build_is_cleaned(env):
    env.build()
    leftovers: list[Path] = []
    env.during_patch.append(lambda out: leftovers.append(out.parent))
    env.tools_updated = True
    env.build()
    scratch_root = leftovers[0].parent
    scratch_root.mkdir(parents=True, exist_ok=True)
    (scratch_root / "killed.apk").write_text("partial")

    env.build()

    assert not (scratch_root / "killed.apk").exists()


# --- criterion 5: download 404 ---------------------------------------------------


def test_download_unknown_name_is_404(env):
    with pytest.raises(HTTPException) as caught:
        main.download_app_by_name("com.nothing-1.0.apk")

    assert caught.value.status_code == 404


def test_download_still_matches_exact_name_and_package_id(env):
    env.build()
    name = env.build_info()["filename"]

    exact = main.download_app_by_name(name)
    by_package = main.download_app_by_name("com.a")

    assert Path(exact.path).name == name
    assert Path(by_package.path).name == name


# --- criterion 6: builds recorded by the old code ----------------------------------


def old_style_build(env: BuildEnv) -> str:
    """Record a build the way the previous code did, with its file on disk."""
    env.patched_dir.mkdir(parents=True)
    (env.patched_dir / "com.a-1.0-t1.apk").write_text("old")
    edit_app(
        "com.a",
        last_successful_build={
            "app_version": "1.0",
            "patch_versions": {THIRD: "t1"},
            "combined_version": "1.0-t1",
            "filename": "com.a-1.0-t1.apk",
            "timestamp": "2026-01-01 00:00:00",
        },
    )
    return "com.a-1.0-t1.apk"


def test_old_build_is_served_until_next_build(env):
    old_style_build(env)

    served = env.served()

    assert served["version"] == "1.0-t1"
    assert served["download_url"].endswith("/download/com.a-1.0-t1.apk")


def test_old_build_is_rebuilt_once_then_settles(env):
    old_style_build(env)

    env.build()
    env.build()

    assert env.calls == 1
    assert re.fullmatch(r"1\.0-t1-[0-9a-f]{8}", env.served()["version"])
    assert [p.name for p in env.patched_dir.iterdir()] == [env.build_info()["filename"]]
