"""patch_app must place each -e/-d selector after the bundle that owns it."""

import json
from types import SimpleNamespace

import pytest

from src import patcher

PACKAGE = "com.x"
BASE_REPO = patcher.BASE_PATCHES_REPO


def write_bundle(repo: str, names: list[str], package: str = PACKAGE) -> str:
    """Create the .mpp/.json pair for a repo and return the .mpp path."""
    paths = patcher.get_source_paths(repo)
    paths[".mpp"].parent.mkdir(parents=True, exist_ok=True)
    paths[".mpp"].write_bytes(b"")
    patches = [{"name": n, "compatiblePackages": {package: None}} for n in names]
    paths[".json"].write_text(json.dumps({"patches": patches}))
    return str(paths[".mpp"])


@pytest.fixture
def run_patch(monkeypatch):
    """Return a callable that runs patch_app and yields the Popen command."""
    captured: list[list[str]] = []

    class FakePopen:
        def __init__(self, command, **kwargs):
            captured.append(command)
            applied = [
                f"Applied: {command[i + 1]}\n"
                for i, arg in enumerate(command)
                if arg == "-e"
            ]
            self.lines = iter(applied)
            self.stdout = SimpleNamespace(readline=lambda: next(self.lines, ""))
            self.returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(patcher.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        patcher.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout=f'package="{PACKAGE}"'),
    )

    def run(sources: list[str], include: list[str], exclude: list[str]) -> list[str]:
        app = {
            "id": PACKAGE,
            "name": "X",
            "include_patches": include,
            "exclude_patches": exclude,
        }
        config = {"sources": [{"name": r, "repo": r, "active": True} for r in sources]}
        tools = {"cli_jar": "bin/cli.jar"}
        patcher.patch_app(app, tools, "in.apk", "out.apk", config)
        return captured[-1][captured[-1].index("out.apk") + 1 :]

    return run


def test_two_sources_each_selector_follows_its_bundle(run_patch):
    base = write_bundle("a/base", ["Clone app"])
    third = write_bundle("b/third", ["Hide app icon"])

    tail = run_patch(["a/base", "b/third"], ["Clone app", "Hide app icon"], [])

    assert tail == [
        "--patches", base, "-e", "Clone app",
        "--patches", third, "-e", "Hide app icon",
    ]  # fmt: skip


def test_name_goes_to_first_compatible_source_only(run_patch):
    first = write_bundle("a/first", ["Shared"])
    write_bundle("b/second", ["Shared", "Other"])

    tail = run_patch(["a/first", "b/second"], ["Shared", "Other"], [])

    second = str(patcher.get_source_paths("b/second")[".mpp"])
    assert tail == [
        "--patches", first, "-e", "Shared",
        "--patches", second, "-e", "Other",
    ]  # fmt: skip


def test_base_fallback_gets_only_unclaimed_names(run_patch):
    third = write_bundle("b/third", ["Hide app icon"])
    base = write_bundle(BASE_REPO, ["Clone app"])

    tail = run_patch(["b/third"], ["Hide app icon", "Clone app"], [])

    assert tail == [
        "--patches", third, "-e", "Hide app icon",
        "--patches", base, "-e", "Clone app",
    ]  # fmt: skip


def test_single_source_matches_previous_command(run_patch):
    only = write_bundle("a/only", ["One", "Two"])

    tail = run_patch(["a/only"], ["One", "Two"], [])

    assert tail == ["--patches", only, "-e", "One", "-e", "Two"]


def test_disabled_patch_is_scoped_to_its_bundle(run_patch):
    first = write_bundle("a/first", ["One", "Off1"])
    second = write_bundle("b/second", ["Two", "Off2"])

    tail = run_patch(["a/first", "b/second"], ["One", "Two"], ["Off2", "Off1"])

    assert tail == [
        "--patches", first, "-e", "One", "-d", "Off1",
        "--patches", second, "-e", "Two", "-d", "Off2",
    ]  # fmt: skip
