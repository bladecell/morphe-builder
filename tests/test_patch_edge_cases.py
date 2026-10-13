"""Edge cases of patch_app command building and its failure rules."""

import json
from pathlib import Path
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
def run_patch(monkeypatch, tmp_path):
    """Return a callable running patch_app against a fake morphe-cli.

    The fake writes the output APK (absolute path) when it starts and prints
    "Applied: <name>" for every -e name unless `cli_lines` overrides the
    output. `read_error` makes the read loop raise. The callable returns
    (result, tail, popen_calls); `run.output` is the output path and
    `run.processes` the fake processes.
    """
    calls: list[list[str]] = []
    processes: list = []
    output = tmp_path / "out.apk"

    def run(
        sources: list[tuple[str, bool]],
        include: list[str],
        exclude: list[str],
        cli_lines: list[str] | None = None,
        returncode: int = 0,
        read_error: Exception | None = None,
    ):
        class FakePopen:
            def __init__(self, command, **kwargs):
                calls.append(command)
                processes.append(self)
                self.kwargs = kwargs
                self.killed = False
                Path(command[command.index("-o") + 1]).write_text("apk")
                if cli_lines is None:
                    names = [command[i + 1] for i, a in enumerate(command) if a == "-e"]
                    lines = [f"Applied: {n}\n" for n in names]
                else:
                    lines = [f"{line}\n" for line in cli_lines]
                self.lines = iter(lines)
                self.stdout = SimpleNamespace(readline=self.readline)
                self.returncode = returncode

            def readline(self):
                if read_error is not None:
                    raise read_error
                return next(self.lines, "")

            def poll(self):
                return None if self.killed is False and read_error else 0

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                return self.returncode

        monkeypatch.setattr(patcher.subprocess, "Popen", FakePopen)
        monkeypatch.setattr(
            patcher.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(stdout=f'package="{PACKAGE}"'),
        )
        app = {
            "id": PACKAGE,
            "name": "AppX",
            "include_patches": include,
            "exclude_patches": exclude,
        }
        config = {
            "sources": [
                {"name": r, "repo": r, "active": active} for r, active in sources
            ]
        }
        result = patcher.patch_app(
            app, {"cli_jar": "bin/cli.jar"}, "in.apk", output, config
        )
        last = calls[-1]
        tail = last[last.index(str(output)) + 1 :]
        return result, tail, calls

    run.output = output
    run.processes = processes
    return run


def test_base_is_source_and_fallback_gives_one_bundle(run_patch):
    base = write_bundle(BASE_REPO, ["Clone app", "Hide icon"])

    (ok, repos), tail, _ = run_patch(
        [(BASE_REPO, True)], ["Clone app", "Hide icon"], []
    )

    assert ok is True
    assert repos == [BASE_REPO]
    assert tail == ["--patches", base, "-e", "Clone app", "-e", "Hide icon"]


def test_base_source_claims_some_and_fallback_merges_rest(run_patch):
    base = write_bundle(BASE_REPO, ["Clone app", "Hide icon"])
    write_bundle("a/own", ["Own"])

    (ok, _), tail, _ = run_patch(
        [("a/own", True), (BASE_REPO, True)], ["Own", "Hide icon"], []
    )

    own = str(patcher.get_source_paths("a/own")[".mpp"])
    assert ok is True
    assert tail == [
        "--patches", own, "-e", "Own",
        "--patches", base, "-e", "Hide icon",
    ]  # fmt: skip


def test_fallback_alone_without_sources(run_patch):
    base = write_bundle(BASE_REPO, ["Clone app"])

    (ok, repos), tail, _ = run_patch([], ["Clone app"], [])

    assert ok is True
    assert repos == [BASE_REPO]
    assert tail == ["--patches", base, "-e", "Clone app"]


def test_no_selection_has_no_selectors_and_still_succeeds(run_patch):
    base = write_bundle(BASE_REPO, ["Clone app"])

    (ok, _), tail, _ = run_patch([], [], [], cli_lines=["Applying 0 patches..."])

    assert ok is True
    assert tail == ["--patches", base]


def test_exclude_not_carried_by_included_bundle_is_dropped(run_patch):
    only = write_bundle("a/only", ["One"])
    write_bundle("b/unused", ["Elsewhere"])

    (_, _), tail, _ = run_patch(
        [("a/only", True), ("b/unused", True)], ["One"], ["Elsewhere"]
    )

    assert tail == ["--patches", only, "-e", "One"]


def test_excluded_and_selected_name_disables_after_enable(run_patch):
    only = write_bundle("a/only", ["One", "Two"])

    (_, _), tail, _ = run_patch([("a/only", True)], ["One", "Two"], ["Two"])

    assert tail == ["--patches", only, "-e", "One", "-e", "Two", "-d", "Two"]


def test_source_carrying_no_selected_name_is_omitted(run_patch):
    write_bundle("a/idle", ["Nothing"])
    used = write_bundle("b/used", ["One"])

    (_, repos), tail, _ = run_patch([("a/idle", True), ("b/used", True)], ["One"], [])

    assert repos == ["b/used"]
    assert tail == ["--patches", used, "-e", "One"]


def test_inactive_source_is_ignored(run_patch):
    write_bundle("a/off", ["One"])
    base = write_bundle(BASE_REPO, ["One"])

    (_, repos), tail, _ = run_patch([("a/off", False)], ["One"], [])

    assert repos == [BASE_REPO]
    assert tail == ["--patches", base, "-e", "One"]


def test_incompatible_package_fails_naming_app_and_patch(run_patch):
    write_bundle("a/other", ["One"], package="com.other")

    with pytest.raises(patcher.PatchSelectionError) as info:
        run_patch([("a/other", True)], ["One"], [])

    assert str(info.value) == "No active patch source provides every selected patch"


def test_exclusive_flag_is_always_passed(run_patch):
    write_bundle("a/only", ["One"])

    _, _, calls = run_patch([("a/only", True)], ["One"], [])

    assert "--exclusive" in calls[-1]


def test_name_matching_ignores_case_and_keeps_saved_spelling(run_patch):
    only = write_bundle("a/only", ["Clone App", "Hide Icon"])

    (ok, _), tail, _ = run_patch([("a/only", True)], ["clone app"], ["HIDE ICON"])

    assert ok is True
    assert tail == ["--patches", only, "-e", "clone app", "-d", "HIDE ICON"]


def test_case_insensitive_exclude_of_selected_name(run_patch):
    only = write_bundle("a/only", ["Clone App"])

    (_, _), tail, _ = run_patch([("a/only", True)], ["Clone App"], ["clone app"])

    assert tail == ["--patches", only, "-e", "Clone App", "-d", "clone app"]


def test_unknown_name_fails_before_running_cli(run_patch):
    write_bundle("a/only", ["One"])

    with pytest.raises(patcher.PatchSelectionError) as info:
        run_patch([("a/only", True)], ["One", "Ghost"], [])

    assert str(info.value) == "No active patch source provides every selected patch"


def test_no_bundle_at_all_with_selection_fails(run_patch):
    with pytest.raises(patcher.PatchSelectionError) as info:
        run_patch([], ["One"], [])

    assert str(info.value) == "No active patch source provides every selected patch"


def test_zero_applied_patches_fails_and_removes_output(run_patch):
    write_bundle("a/only", ["One"])

    with pytest.raises(patcher.PatchSelectionError) as info:
        run_patch([("a/only", True)], ["One"], [], cli_lines=["Applying 0 patches..."])

    assert str(info.value) == "Selected patches were not applied"
    assert not run_patch.output.exists()


def test_zero_applied_error_does_not_reflect_cli_lines(run_patch):
    write_bundle("a/only", ["One"])
    lines = [f"line {n}" for n in range(1, 9)]

    with pytest.raises(patcher.PatchSelectionError) as info:
        run_patch([("a/only", True)], ["One"], [], cli_lines=lines)

    assert str(info.value) == "Selected patches were not applied"


@pytest.mark.parametrize(
    "line",
    ["Applied: One", "INFO: Applied: One", "  Applied: One  "],
)
def test_applied_line_formats_that_count(run_patch, line):
    write_bundle("a/only", ["One"])

    (ok, _), _, _ = run_patch([("a/only", True)], ["One"], [], cli_lines=[line])

    assert ok is True


@pytest.mark.parametrize(
    "line",
    [
        "Not Applied: One",
        "Skipped, Applied: nothing",
        "Applied:",
        "Applying 0 patches... Applied: ",
        "Failed to apply. Applied: One was rolled back",
    ],
)
def test_other_lines_do_not_count_as_applied(run_patch, line):
    write_bundle("a/only", ["One"])

    with pytest.raises(patcher.PatchSelectionError):
        run_patch([("a/only", True)], ["One"], [], cli_lines=[line])


def test_severe_with_zero_applied_reports_severe(run_patch):
    write_bundle("a/only", ["One"])

    (ok, repos), _, _ = run_patch(
        [("a/only", True)], ["One"], [], cli_lines=["SEVERE: boom"]
    )

    assert (ok, repos) == (False, [])
    assert not run_patch.output.exists()


def test_severe_with_applied_removes_output(run_patch):
    write_bundle("a/only", ["One"])

    (ok, _), _, _ = run_patch(
        [("a/only", True)], ["One"], [], cli_lines=["Applied: One", "SEVERE: boom"]
    )

    assert ok is False
    assert not run_patch.output.exists()


def test_non_zero_exit_fails_and_removes_output(run_patch):
    write_bundle("a/only", ["One"])

    (ok, repos), _, _ = run_patch([("a/only", True)], ["One"], [], returncode=1)

    assert (ok, repos) == (False, [])
    assert not run_patch.output.exists()


def test_read_error_kills_child_and_removes_output(run_patch):
    write_bundle("a/only", ["One"])

    with pytest.raises(UnicodeDecodeError):
        run_patch(
            [("a/only", True)],
            ["One"],
            [],
            read_error=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
        )

    assert run_patch.processes[-1].killed is True
    assert not run_patch.output.exists()


def test_cli_output_is_decoded_leniently(run_patch):
    write_bundle("a/only", ["One"])

    _, _, _ = run_patch([("a/only", True)], ["One"], [])

    assert run_patch.processes[-1].kwargs["errors"] == "replace"


def test_base_json_missing_names_the_cause(run_patch):
    write_bundle(BASE_REPO, ["One"])
    patcher.get_source_paths(BASE_REPO)[".json"].unlink()

    with pytest.raises(patcher.PatchListError) as info:
        run_patch([], ["One"], [])

    assert BASE_REPO in str(info.value)
    assert "missing" in str(info.value)


def test_unreadable_source_list_names_the_cause(run_patch):
    write_bundle("a/broken", ["One"])
    patcher.get_source_paths("a/broken")[".json"].write_text("{not json")

    with pytest.raises(patcher.PatchListError) as info:
        run_patch([("a/broken", True)], ["One"], [])

    assert "a/broken" in str(info.value)


def test_null_patches_list_is_a_patch_list_error(run_patch):
    write_bundle("a/nulls", ["One"])
    patcher.get_source_paths("a/nulls")[".json"].write_text('{"patches": null}')

    with pytest.raises(patcher.PatchListError):
        run_patch([("a/nulls", True)], ["One"], [])


def test_some_applied_is_success(run_patch):
    write_bundle("a/only", ["One", "Two"])

    (ok, _), _, _ = run_patch(
        [("a/only", True)],
        ["One", "Two"],
        [],
        cli_lines=["Applied: One", "Skipped: Two"],
    )

    assert ok is True


def test_pipeline_records_failed_with_message_and_keeps_old_apk(
    data_dir, pipeline, monkeypatch
):
    """A PatchSelectionError ends in the Failed path with the app named."""
    from tests.conftest import read_config, run_build

    old_apk = data_dir / "apks" / "patched" / "com.a-0.9-old.apk"
    old_apk.parent.mkdir(parents=True, exist_ok=True)
    old_apk.write_text("old")

    def refuse(app, tools, raw, out, config):
        raise patcher.PatchSelectionError(f"{app['name']}: no patch was applied")

    monkeypatch.setattr(patcher, "patch_app", refuse)

    run_build(["com.a"])

    app = next(a for a in read_config(data_dir)["apps"] if a["id"] == "com.a")
    assert app["status"] == "Failed"
    assert app["error"] == "A: no patch was applied"
    assert old_apk.read_text() == "old"
