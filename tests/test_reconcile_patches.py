"""Tests for automatic patch selection reconcile after a source update."""

from unittest.mock import patch

from src.patcher import reconcile_app_patches

REPO = "owner/patches"


def _records(*pairs: tuple[str, bool]) -> list[dict]:
    return [{"name": name, "default": default} for name, default in pairs]


def _config(known: dict | None) -> dict:
    app = {"id": "com.x", "include_patches": {REPO: ["Keep", "Gone"]}}
    if known is not None:
        app["known_patches"] = known
    return {"sources": [{"repo": REPO, "active": True}], "apps": [app]}


def test_removed_deselected_and_new_default_selected() -> None:
    config = _config({REPO: ["Keep", "Gone", "Old off"]})
    records = _records(("Keep", True), ("New on", True), ("New off", False))
    with patch("src.patcher.compatible_patches", return_value=records):
        reconcile_app_patches(config)
    app = config["apps"][0]
    assert app["include_patches"][REPO] == ["Keep", "New on"]
    assert app["known_patches"][REPO] == ["Keep", "New on", "New off"]


def test_without_baseline_only_removals_apply() -> None:
    config = _config(None)
    records = _records(("Keep", True), ("New on", True))
    with patch("src.patcher.compatible_patches", return_value=records):
        reconcile_app_patches(config)
    assert config["apps"][0]["include_patches"][REPO] == ["Keep"]


def test_user_deselected_known_patch_stays_off() -> None:
    config = _config({REPO: ["Keep", "Gone", "Opt"]})
    config["apps"][0]["include_patches"][REPO] = ["Keep"]
    records = _records(("Keep", True), ("Opt", True))
    with patch("src.patcher.compatible_patches", return_value=records):
        reconcile_app_patches(config)
    assert config["apps"][0]["include_patches"][REPO] == ["Keep"]
