"""The build-layout marker forces one rebuild of builds made before it."""

from pathlib import Path

from src import patcher

SELECTION = {"include": ["p"], "exclude": []}
APP = {"id": "com.a", "include_patches": ["p"], "exclude_patches": []}


def make_record(tmp_path: Path, **overrides) -> dict:
    """Return a last_successful_build that is current except for overrides."""
    (tmp_path / "a.apk").write_text("apk")
    record = {
        "app_version": "1.0",
        "patch_versions": {"own/src": "v1"},
        "filename": "a.apk",
        "patches": SELECTION,
        "build_layout": patcher.BUILD_LAYOUT,
    }
    record.update(overrides)
    return record


def is_current(tmp_path: Path, record: dict) -> bool:
    return patcher.is_build_current(record, "1.0", APP, {"own/src": "v1"}, tmp_path)


def test_record_with_current_layout_is_current(tmp_path):
    assert is_current(tmp_path, make_record(tmp_path)) is True


def test_record_without_layout_is_not_current(tmp_path):
    record = make_record(tmp_path)
    del record["build_layout"]

    assert is_current(tmp_path, record) is False


def test_record_with_older_layout_is_not_current(tmp_path):
    record = make_record(tmp_path, build_layout=patcher.BUILD_LAYOUT - 1)

    assert is_current(tmp_path, record) is False


def test_marker_changes_the_version_hash(monkeypatch):
    before = patcher.build_combined_version("1.0", {"own/src": "v1"}, SELECTION)
    monkeypatch.setattr(patcher, "BUILD_LAYOUT", patcher.BUILD_LAYOUT + 1)

    after = patcher.build_combined_version("1.0", {"own/src": "v1"}, SELECTION)

    assert before != after
