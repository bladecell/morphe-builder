"""Source-aware patch selection stays deterministic across the pipeline."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import patcher

PACKAGE = "com.example.app"


def write_source(
    repo: str,
    patches: list[dict[str, object]],
    *,
    bundle: bool = True,
) -> Path:
    """Write a local patch list and optionally its matching bundle."""
    paths = patcher.get_source_paths(repo)
    paths[".json"].parent.mkdir(parents=True, exist_ok=True)
    paths[".json"].write_text(json.dumps({"patches": patches}))
    if bundle:
        paths[".mpp"].write_bytes(b"bundle")
    return paths[".mpp"]


def patch(name: str, versions: list[str]) -> dict[str, object]:
    return {"name": name, "compatiblePackages": {PACKAGE: versions}}


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Capture a fake morphe-cli command and report its selected patches applied."""
    commands: list[list[str]] = []

    class FakeProcess:
        def __init__(self, command: list[str], **_kwargs: object) -> None:
            commands.append(command)
            output_path = Path(command[command.index("-o") + 1])
            output_path.write_bytes(b"patched")
            names = [
                command[index + 1] for index, arg in enumerate(command) if arg == "-e"
            ]
            self.lines = iter([f"Applied: {name}\n" for name in names])
            self.stdout = SimpleNamespace(readline=lambda: next(self.lines, ""))
            self.returncode = 0

        def poll(self) -> int:
            return 0

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(patcher.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(
        patcher,
        "run_child",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=f'package="{PACKAGE}"'),
    )
    return commands


def invoke_patch(
    app: dict,
    sources: list[dict[str, object]],
    output: Path,
) -> tuple[bool, list[str]]:
    return patcher.patch_app(
        app,
        {"cli_jar": "bin/cli.jar"},
        Path("input.apk"),
        output,
        {"sources": sources},
    )


def test_mapping_selects_same_named_patches_from_both_repos(cli, tmp_path):
    first = write_source("one/patches", [patch("Same", ["1.0"])])
    second = write_source("two/patches", [patch("Same", ["2.0"])])
    config_sources = [
        {"repo": "one/patches", "active": True},
        {"repo": "two/patches", "active": True},
    ]
    app = {
        "id": PACKAGE,
        "name": "Example",
        "include_patches": {"one/patches": ["Same"], "two/patches": ["Same"]},
        "exclude_patches": [],
    }

    success, repos = invoke_patch(app, config_sources, tmp_path / "out.apk")

    command = cli[0]
    assert success
    assert repos == ["one/patches", "two/patches"]
    second_bundle_arg = command.index("--patches", command.index("--patches") + 1)
    assert command[second_bundle_arg + 1] == str(second)
    assert command.count("--patches") == 2
    assert command[command.index("--patches") + 1] == str(first)
    assert command.count("-e") == 2


def test_legacy_name_resolves_to_first_configured_compatible_source(cli, tmp_path):
    first = write_source("one/patches", [patch("Same", ["1.0"])])
    write_source("two/patches", [patch("Same", ["2.0"])])
    sources = [
        {"repo": "one/patches", "active": True},
        {"repo": "two/patches", "active": True},
    ]

    success, repos = invoke_patch(
        {"id": PACKAGE, "name": "Example", "include_patches": ["same"]},
        sources,
        tmp_path / "out.apk",
    )

    assert success
    assert repos == ["one/patches"]
    assert cli[0][cli[0].index("--patches") + 1] == str(first)


def test_mapped_versions_only_use_sources_with_selected_patches(data_dir):
    write_source("one/patches", [patch("One", ["1.0", "2.0"]), patch("Other", ["9.0"])])
    write_source("two/patches", [patch("Two", ["3.0"]), patch("Not selected", ["9.0"])])
    config = {
        "sources": [
            {"repo": "one/patches", "active": True},
            {"repo": "two/patches", "active": True},
        ],
        "apps": [{"id": PACKAGE, "include_patches": {"two/patches": ["Two"]}}],
    }

    versions, _, _ = patcher.get_supported_versions(PACKAGE, config, quiet=True)

    assert versions == ["9.0"]


def test_unreadable_unselected_source_does_not_block_mapped_selection(data_dir):
    broken = write_source("one/broken", [patch("One", ["1.0"])])
    broken.with_suffix(".json").write_text("{")
    write_source("two/patches", [patch("Two", ["3.0"])])
    config = {
        "sources": [
            {"repo": "one/broken", "active": True},
            {"repo": "two/patches", "active": True},
        ],
        "apps": [{"id": PACKAGE, "include_patches": {"two/patches": ["Two"]}}],
    }

    assert patcher.get_supported_versions(PACKAGE, config, quiet=True)[0] == ["3.0"]


def test_legacy_resolution_stops_before_unrelated_unreadable_source(data_dir):
    write_source("one/patches", [patch("One", ["1.0"])])
    broken = write_source("two/broken", [patch("Two", ["2.0"])])
    broken.with_suffix(".json").write_text("{")
    config = {
        "sources": [
            {"repo": "one/patches", "active": True},
            {"repo": "two/broken", "active": True},
        ],
        "apps": [{"id": PACKAGE, "include_patches": ["One"]}],
    }

    assert patcher.get_supported_versions(PACKAGE, config, quiet=True)[0] == ["1.0"]


def test_unreadable_earlier_source_blocks_unresolved_legacy_name(data_dir):
    broken = write_source("one/broken", [patch("One", ["1.0"])])
    broken.with_suffix(".json").write_text("{")
    write_source("two/provider", [patch("Two", ["2.0"])])
    config = {
        "sources": [
            {"repo": "one/broken", "active": True},
            {"repo": "two/provider", "active": True},
        ],
        "apps": [{"id": PACKAGE, "include_patches": ["Two"]}],
    }

    with pytest.raises(patcher.PatchListError, match="one/broken"):
        patcher.get_supported_versions(PACKAGE, config, quiet=True)


def test_unreadable_selected_mapping_source_fails(data_dir):
    broken = write_source("one/broken", [patch("One", ["1.0"])])
    broken.with_suffix(".json").write_text("{")
    config = {
        "sources": [{"repo": "one/broken", "active": True}],
        "apps": [{"id": PACKAGE, "include_patches": {"one/broken": ["One"]}}],
    }

    with pytest.raises(patcher.PatchListError, match="one/broken"):
        patcher.get_supported_versions(PACKAGE, config, quiet=True)


def test_inactive_and_deleted_mapped_sources_are_skipped_with_warning(data_dir, capsys):
    write_source("active/provider", [patch("Active", ["1.0"])])
    app = {
        "id": PACKAGE,
        "include_patches": {
            "inactive/provider": ["Inactive"],
            "deleted/provider": ["Deleted"],
            "active/provider": ["Active"],
        },
    }
    config = {
        "sources": [
            {"repo": "inactive/provider", "active": False},
            {"repo": "active/provider", "active": True},
        ],
    }

    resolved = patcher.resolve_patch_selection(app, config)
    warning = capsys.readouterr().out

    assert resolved == {"active/provider": ["Active"]}
    assert "inactive/provider" not in warning
    assert "deleted/provider" not in warning
    assert "Inactive" not in warning
    assert "Deleted" not in warning


def test_deleted_source_build_warning_does_not_echo_repo_or_patch_guess(
    capsys, tmp_path
):
    private_repo = "deleted/private-repository"
    private_name = "private-patch-guess"
    app = {
        "id": PACKAGE,
        "name": "Example",
        "include_patches": {private_repo: [private_name]},
        "exclude_patches": [],
    }

    with pytest.raises(patcher.PatchSelectionError):
        invoke_patch(app, [], tmp_path / "unused.apk")

    build_warning = capsys.readouterr().out
    assert private_repo not in build_warning
    assert private_name not in build_warning


@pytest.mark.parametrize(
    "repo,guess",
    [
        ("owner/private-repo", "private-patch-guess"),
        ("another/hidden-source", "other-secret-guess"),
    ],
)
def test_unresolved_selection_diagnostic_does_not_reflect_patch_or_repo(
    data_dir, repo, guess
):
    write_source(repo, [patch("Some other patch", ["1.0"])])
    app = {
        "id": PACKAGE,
        "name": "Example",
        "include_patches": {repo: [guess]},
    }
    config = {"sources": [{"repo": repo, "active": True}]}

    with pytest.raises(patcher.PatchSelectionError) as error:
        patcher.resolve_patch_selection(app, config)

    assert str(error.value) == "Selected patch is unavailable"
    assert guess not in str(error.value)
    assert repo not in str(error.value)


def test_all_inactive_mapped_selection_fails_patch_before_cli(cli, tmp_path):
    app = {
        "id": PACKAGE,
        "name": "Example",
        "include_patches": {"inactive/provider": ["Patch"]},
    }

    with pytest.raises(patcher.PatchSelectionError, match="none of the selected"):
        invoke_patch(
            app,
            [{"repo": "inactive/provider", "active": False}],
            tmp_path / "out.apk",
        )

    assert cli == []


def test_inactive_mapped_selection_fails_before_pinned_version_return(data_dir):
    app = {
        "id": PACKAGE,
        "version": "7.7",
        "include_patches": {"deleted/provider": ["Patch"]},
    }
    config = {"sources": [], "apps": [app]}

    with pytest.raises(patcher.PatchSelectionError):
        patcher.get_supported_versions(PACKAGE, config, quiet=True)


@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("raw_apk_exists", [False, True])
def test_inapplicable_selection_stops_pipeline_before_scraper_and_keeps_old_apk(
    data_dir, monkeypatch, pinned, raw_apk_exists
):
    app = {
        "id": PACKAGE,
        "name": "Example",
        "include_patches": {"deleted/provider": ["Patch"]},
        "last_successful_build": {"filename": f"{PACKAGE}-old.apk"},
    }
    if pinned:
        app["version"] = "7.7"
    config = {"sources": [], "apps": [app], "tools": {}, "settings": {}}
    monkeypatch.setattr(patcher, "load_config", lambda: config)
    monkeypatch.setattr(patcher, "update_tools", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(patcher, "get_patch_versions", lambda _config: {})
    results: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        patcher,
        "save_app_result",
        lambda app_id, **fields: results.append((app_id, fields)),
    )

    patched_dir = data_dir / "apks" / "patched"
    patched_dir.mkdir(parents=True)
    old_apk = patched_dir / f"{PACKAGE}-old.apk"
    old_apk.write_bytes(b"previous apk")
    if raw_apk_exists:
        raw_dir = data_dir / "apks" / "raw"
        raw_dir.mkdir(parents=True)
        (raw_dir / f"{PACKAGE}-7.7.apk").write_bytes(b"raw apk")

    def forbidden_scraper(**_kwargs):
        raise AssertionError("scraper initialized despite inapplicable selection")

    monkeypatch.setattr(patcher, "ApkMirror_Downloader", forbidden_scraper)
    monkeypatch.setattr(
        patcher,
        "download_apk",
        lambda *_args, **_kwargs: pytest.fail("download called for invalid selection"),
    )
    monkeypatch.setattr(
        patcher,
        "patch_app",
        lambda *_args, **_kwargs: pytest.fail("patcher called for invalid selection"),
    )

    patcher.run_pipeline([PACKAGE], quiet=True)

    assert old_apk.read_bytes() == b"previous apk"
    assert app["last_successful_build"] == {"filename": old_apk.name}
    assert results and results[0][1]["status"] == "Error"


@pytest.mark.parametrize("selected_repos", [["a-b/c"], ["a-b/c", "a/b-c"]])
def test_active_path_aliases_reject_mapped_selection(data_dir, selected_repos):
    write_source("a-b/c", [patch("Alpha", ["1.0"]), patch("Beta", ["2.0"])])
    sources = [
        {"repo": "a-b/c", "active": True},
        {"repo": "a/b-c", "active": True},
    ]
    app = {
        "id": PACKAGE,
        "include_patches": {repo: ["Alpha"] for repo in selected_repos},
    }

    with pytest.raises(patcher.PatchSelectionError, match="not usable"):
        patcher.resolve_patch_selection(app, {"sources": sources})


def test_active_path_alias_rejects_legacy_provider_selection(data_dir):
    write_source("a-b/c", [patch("Alpha", ["1.0"])])
    config = {
        "sources": [
            {"repo": "a-b/c", "active": True},
            {"repo": "a/b-c", "active": True},
        ],
        "apps": [{"id": PACKAGE, "include_patches": ["Alpha"]}],
    }

    with pytest.raises(patcher.PatchSelectionError, match="not usable"):
        patcher.get_supported_versions(PACKAGE, config, quiet=True)


def test_unselected_active_path_alias_does_not_block_other_mapped_source(data_dir):
    write_source("a-b/c", [patch("Alias", ["1.0"])])
    write_source("other/source", [patch("Selected", ["2.0"])])
    config = {
        "sources": [
            {"repo": "a-b/c", "active": True},
            {"repo": "a/b-c", "active": True},
            {"repo": "other/source", "active": True},
        ],
        "apps": [{"id": PACKAGE, "include_patches": {"other/source": ["Selected"]}}],
    }

    versions, _, _ = patcher.get_supported_versions(PACKAGE, config, quiet=True)

    assert versions == ["2.0"]


def test_exclusion_stays_with_first_bundle_carrying_its_name(cli, tmp_path):
    write_source("one/patches", [patch("Same", ["1.0"])])
    write_source("two/patches", [patch("Same", ["2.0"])])
    app = {
        "id": PACKAGE,
        "name": "Example",
        "include_patches": {"one/patches": ["Same"], "two/patches": ["Same"]},
        "exclude_patches": ["Same"],
    }
    sources = [
        {"repo": "one/patches", "active": True},
        {"repo": "two/patches", "active": True},
    ]

    invoke_patch(app, sources, tmp_path / "out.apk")

    command = cli[0]
    first_bundle = command.index("--patches")
    second_bundle = command.index("--patches", first_bundle + 1)
    assert command[first_bundle + 1 : second_bundle] == [
        str(patcher.get_source_paths("one/patches")[".mpp"]),
        "-e",
        "Same",
        "-d",
        "Same",
    ]
    assert "-d" not in command[second_bundle + 1 :]


def test_mapped_missing_bundle_fails_instead_of_falling_back(cli, tmp_path):
    write_source("one/no-bundle", [patch("One", ["1.0"])], bundle=False)
    write_source("two/fallback", [patch("One", ["2.0"])])
    sources = [
        {"repo": "one/no-bundle", "active": True},
        {"repo": "two/fallback", "active": True},
    ]
    app = {
        "id": PACKAGE,
        "name": "Example",
        "include_patches": {"one/no-bundle": ["One"]},
    }

    with pytest.raises(patcher.PatchListError, match="one/no-bundle"):
        invoke_patch(app, sources, tmp_path / "out.apk")

    assert cli == []


def test_map_order_does_not_change_canonical_selection_hash(data_dir):
    left = {"include": {"a/source": ["A", "B"], "b/source": ["Same"]}, "exclude": []}
    right = {"include": {"b/source": ["Same"], "a/source": ["B", "A"]}, "exclude": []}

    assert patcher.build_combined_version(
        "1.0", {}, left
    ) == patcher.build_combined_version("1.0", {}, right)


def test_casefold_duplicate_mapping_keys_are_rejected(data_dir):
    app = {"id": PACKAGE, "include_patches": {"One/Repo": ["A"], "one/repo": ["B"]}}
    config = {"sources": [{"repo": "one/repo", "active": True}], "apps": [app]}

    with pytest.raises(patcher.PatchSelectionError):
        patcher.resolve_patch_selection(app, config)


def test_configured_repo_spelling_is_canonical_in_build_record(data_dir):
    source = {"repo": "Owner/Repo", "active": True}
    app = {"id": PACKAGE, "include_patches": {"owner/repo": ["Patch"]}}

    assert patcher.selected_patches(app, {"sources": [source]})["include"] == {
        "Owner/Repo": ["Patch"]
    }


def test_build_is_stale_when_selected_active_source_was_not_recorded(data_dir):
    app = {"id": PACKAGE, "include_patches": {"two/source": ["Patch"]}}
    build = {
        "filename": "old.apk",
        "build_layout": patcher.BUILD_LAYOUT,
        "app_version": "1.0",
        "patches": {"include": {"two/source": ["Patch"]}, "exclude": []},
        "patch_versions": {"one/source": "v1"},
    }
    (data_dir / "apks" / "patched").mkdir(parents=True)
    (data_dir / "apks" / "patched" / "old.apk").write_bytes(b"old")

    assert not patcher.is_build_current(
        build,
        "1.0",
        app,
        {"one/source": "v1", "two/source": "v1"},
        data_dir / "apks" / "patched",
        {"sources": [{"repo": "two/source", "active": True}]},
    )


def test_pipeline_records_hash_and_keeps_legacy_config_list(data_dir, monkeypatch):
    repo = "one/patches"
    write_source(repo, [patch("Patch", ["1.0"])])
    app = {
        "id": PACKAGE,
        "name": "Example",
        "include_patches": ["Patch"],
        "exclude_patches": [],
    }
    config = {
        "sources": [{"repo": repo, "active": True}],
        "apps": [app],
        "tools": {"cli_jar": "bin/cli.jar"},
        "settings": {},
    }
    monkeypatch.setattr(patcher, "load_config", lambda: config)
    monkeypatch.setattr(patcher, "update_tools", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(patcher, "get_patch_versions", lambda _config: {repo: "v1"})
    monkeypatch.setattr(
        patcher,
        "get_supported_versions",
        lambda *_args, **_kwargs: (["1.0"], None, None),
    )
    monkeypatch.setattr(
        patcher,
        "download_apk",
        lambda _pkg, _version, path, *_args, **_kwargs: Path(path).write_bytes(b"raw"),
    )
    monkeypatch.setattr(patcher, "extract_app_metadata", lambda *_args, **_kwargs: None)

    def patch_app(_app, _tools, _raw, output, _config):
        Path(output).write_bytes(b"patched")
        return True, [repo]

    monkeypatch.setattr(patcher, "patch_app", patch_app)
    records: list[dict] = []
    monkeypatch.setattr(
        patcher,
        "save_app_result",
        lambda _app_id, **fields: (
            records.append(fields["last_successful_build"])
            if "last_successful_build" in fields
            else None
        ),
    )

    patcher.run_pipeline([PACKAGE], quiet=True, scraper=object())

    assert app["include_patches"] == ["Patch"]
    assert records[0]["patches"]["include"] == ["Patch"]
    assert records[0]["combined_version"] == patcher.build_combined_version(
        "1.0", {repo: "v1"}, patcher.selected_patches(app, config)
    )
