"""HTTP contract for opt-in source-grouped patch catalogs and mapped saves."""

import json

import pytest
from fastapi.testclient import TestClient

from src import config_store, main, patcher
from tests.conftest import make_config

PACKAGE = "com.example.app"


@pytest.fixture
def client(data_dir) -> TestClient:
    return TestClient(main.app)


def write_source(repo: str, patches: list[dict]) -> None:
    paths = patcher.get_source_paths(repo)
    paths[".json"].parent.mkdir(parents=True, exist_ok=True)
    paths[".json"].write_text(json.dumps({"patches": patches}))
    paths[".mpp"].write_bytes(b"bundle")


def patch(name: str, package: str = PACKAGE, description: str | None = None) -> dict:
    result = {"name": name, "compatiblePackages": {package: ["1.0"]}}
    if description is not None:
        result["description"] = description
    return result


def save_config(config: dict) -> None:
    config_store.save_config(config)


def test_legacy_patch_catalog_and_flat_add_payload_stay_compatible(client, data_dir):
    repo = "owner/patches"
    write_source(
        repo, [patch("Same", description="legacy"), patch("Other", "com.other")]
    )
    config = make_config()
    config["sources"] = [{"name": "Owner Patches", "repo": repo, "active": True}]
    config["apps"] = [
        {
            "id": PACKAGE,
            "name": "Example",
            "include_patches": ["Same"],
            "exclude_patches": ["Keep excluded"],
        }
    ]
    save_config(config)

    response = client.get(f"/api/ui/patches/{PACKAGE}")
    saved = client.post(
        "/api/ui/add_app",
        json={"package_id": PACKAGE, "name": "Renamed", "include_patches": ["Same"]},
    )

    assert response.status_code == 200
    assert response.json() == [
        {
            "name": "Same",
            "description": "legacy",
            "enabled_by_default": True,
            "source": "Owner Patches",
            "is_base": False,
        }
    ]
    assert saved.status_code == 200
    current = config_store.load_config()["apps"][0]
    assert current["include_patches"] == ["Same"]
    assert current["exclude_patches"] == ["Keep excluded"]


def test_grouped_catalog_keeps_same_named_patches_separate_by_repo(client):
    first, second = "first/patches", "second/patches"
    write_source(first, [patch("Same", description="first copy")])
    write_source(second, [patch("Same", description="second copy")])
    config = make_config()
    config["sources"] = [
        {"name": "First", "repo": first, "active": True},
        {"name": "Second", "repo": second, "active": True},
    ]
    config["apps"] = [
        {
            "id": PACKAGE,
            "name": "Example",
            "include_patches": {first: ["Same"], second: ["Same"]},
            "exclude_patches": [],
        }
    ]
    save_config(config)

    response = client.get(f"/api/ui/patches/{PACKAGE}?format=grouped")

    assert response.status_code == 200
    body = response.json()
    assert [source["repo"] for source in body["sources"]] == [first, second]
    assert [source["patches"][0]["description"] for source in body["sources"]] == [
        "first copy",
        "second copy",
    ]
    assert body["selection"] == {
        "stored": {first: ["Same"], second: ["Same"]},
        "resolved": {first: ["Same"], second: ["Same"]},
        "status": "resolved",
        "unresolved": [],
        "error": None,
    }


def test_grouped_catalog_matches_stored_patch_names_case_insensitively(client):
    repo = "owner/patches"
    write_source(repo, [patch("Canonical Name")])
    config = make_config()
    config["sources"] = [{"name": "Owner", "repo": repo, "active": True}]
    config["apps"] = [
        {
            "id": PACKAGE,
            "name": "Example",
            "include_patches": {repo: ["cANONICAL nAME"]},
            "exclude_patches": [],
        }
    ]
    save_config(config)

    response = client.get(f"/api/ui/patches/{PACKAGE}?format=grouped")

    assert response.status_code == 200
    source = response.json()["sources"][0]
    assert source["patches"] == [
        {
            "name": "Canonical Name",
            "description": "No description available.",
            "enabled_by_default": True,
            "source": "Owner",
            "is_base": False,
            "selected": True,
        }
    ]
    assert response.json()["selection"]["unresolved"] == []


def test_grouped_catalog_marks_unreadable_source_and_keeps_healthy_catalog(client):
    bad, good = "bad/patches", "good/patches"
    bad_json = patcher.get_source_paths(bad)[".json"]
    bad_json.parent.mkdir(parents=True, exist_ok=True)
    bad_json.write_text("{")
    write_source(good, [patch("Healthy")])
    config = make_config()
    config["sources"] = [
        {"name": "Bad", "repo": bad, "active": True},
        {"name": "Good", "repo": good, "active": True},
    ]
    config["apps"] = [
        {"id": PACKAGE, "name": "Example", "include_patches": [], "exclude_patches": []}
    ]
    save_config(config)

    response = client.get(f"/api/ui/patches/{PACKAGE}?format=grouped")

    assert response.status_code == 200
    sources = response.json()["sources"]
    assert [(source["repo"], source["status"]) for source in sources] == [
        (bad, "unreadable"),
        (good, "available"),
    ]
    assert [patch_entry["name"] for patch_entry in sources[1]["patches"]] == ["Healthy"]
    assert response.json()["selection"]["error"] is None


def test_partial_selection_keeps_healthy_provider_when_another_is_unreadable(client):
    broken, good = "broken/patches", "good/patches"
    broken_json = patcher.get_source_paths(broken)[".json"]
    broken_json.parent.mkdir(parents=True, exist_ok=True)
    broken_json.write_text("{")
    write_source(good, [patch("Healthy")])
    config = make_config()
    config["sources"] = [
        {"name": "Broken", "repo": broken, "active": True},
        {"name": "Good", "repo": good, "active": True},
    ]
    config["apps"] = [
        {
            "id": PACKAGE,
            "name": "Example",
            "include_patches": {broken: ["Old"], good: ["Healthy"]},
            "exclude_patches": [],
        }
    ]
    save_config(config)

    body = client.get(f"/api/ui/patches/{PACKAGE}?format=grouped").json()

    assert body["selection"]["resolved"] == {good: ["Healthy"]}
    assert body["selection"]["unresolved"] == [
        {"repo": broken, "name": "Old", "reason": "source_unavailable"}
    ]


def test_grouped_legacy_selection_uses_resolver_provider_for_selected_flag(client):
    first, second = "first/patches", "second/patches"
    first_json = patcher.get_source_paths(first)[".json"]
    first_json.parent.mkdir(parents=True, exist_ok=True)
    first_json.write_text("{")
    write_source(second, [patch("Shared")])
    config = make_config()
    config["sources"] = [
        {"name": "First", "repo": first, "active": True},
        {"name": "Second", "repo": second, "active": True},
    ]
    config["apps"] = [
        {
            "id": PACKAGE,
            "name": "Example",
            "include_patches": ["Shared"],
            "exclude_patches": [],
        }
    ]
    save_config(config)

    body = client.get(f"/api/ui/patches/{PACKAGE}?format=grouped").json()

    assert body["selection"]["resolved"] == {}
    assert body["sources"][1]["patches"][0]["selected"] is False
    assert body["selection"]["unresolved"] == [
        {"repo": None, "name": "Shared", "reason": "source_unavailable"}
    ]


def test_legacy_preview_does_not_fallback_to_implicit_base_after_unknown_source(
    client,
):
    earlier = "unreadable/source"
    json_path = patcher.get_source_paths(earlier)[".json"]
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text("{")
    write_source(patcher.BASE_PATCHES_REPO, [patch("Shared")])
    config = make_config()
    config["sources"] = [{"name": "Earlier", "repo": earlier, "active": True}]
    config["apps"] = [
        {
            "id": PACKAGE,
            "name": "Example",
            "include_patches": ["Shared"],
            "exclude_patches": [],
        }
    ]
    save_config(config)

    selection = client.get(f"/api/ui/patches/{PACKAGE}?format=grouped").json()[
        "selection"
    ]

    assert selection["resolved"] == {}
    assert selection["unresolved"] == [
        {"repo": None, "name": "Shared", "reason": "source_unavailable"}
    ]


def test_grouped_resolved_names_use_catalog_spelling(client):
    repo = "owner/patches"
    write_source(repo, [patch("Canonical Name")])
    config = make_config()
    config["sources"] = [{"name": "Owner", "repo": repo, "active": True}]
    config["apps"] = [
        {
            "id": PACKAGE,
            "name": "Example",
            "include_patches": {repo: ["cANONICAL nAME"]},
            "exclude_patches": [],
        }
    ]
    save_config(config)

    selection = client.get(f"/api/ui/patches/{PACKAGE}?format=grouped").json()[
        "selection"
    ]

    assert selection["resolved"] == {repo: ["Canonical Name"]}


def test_partial_preview_keeps_healthy_selection_beside_ambiguous_source(client):
    ambiguous, healthy = "ambiguous/source", "healthy/source"
    write_source(ambiguous, [patch("Choice"), patch("choice")])
    write_source(healthy, [patch("Healthy")])
    config = make_config()
    config["sources"] = [
        {"name": "Ambiguous", "repo": ambiguous, "active": True},
        {"name": "Healthy", "repo": healthy, "active": True},
    ]
    config["apps"] = [
        {
            "id": PACKAGE,
            "name": "Example",
            "include_patches": {ambiguous: ["Choice"], healthy: ["Healthy"]},
            "exclude_patches": [],
        }
    ]
    save_config(config)

    body = client.get(f"/api/ui/patches/{PACKAGE}?format=grouped").json()

    assert body["selection"]["resolved"] == {healthy: ["Healthy"]}
    assert body["selection"]["unresolved"] == [
        {"repo": ambiguous, "name": "Choice", "reason": "patch_unavailable"}
    ]


@pytest.mark.parametrize("missing", ["bundle", "alias"])
def test_mapped_post_rejects_new_selection_for_unbuildable_source(client, missing):
    repo = "a/b-c" if missing == "alias" else "owner/patches"
    write_source(repo, [patch("Selected")])
    config = make_config()
    config["sources"] = [{"name": "Owner", "repo": repo, "active": True}]
    config["apps"] = []
    if missing == "bundle":
        patcher.get_source_paths(repo)[".mpp"].unlink()
    else:
        config["sources"].append({"name": "Alias", "repo": "a-b/c", "active": True})
    save_config(config)

    response = client.post(
        "/api/ui/add_app",
        json={
            "package_id": PACKAGE,
            "name": "Example",
            "include_patches": {repo: ["Selected"]},
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid patch selection"
    assert config_store.load_config()["apps"] == []


def test_grouped_catalog_exposes_deleted_and_inactive_selection_for_roundtrip(client):
    inactive = "inactive/patches"
    write_source(inactive, [patch("Not loaded")])
    config = make_config()
    config["sources"] = [{"name": "Inactive", "repo": inactive, "active": False}]
    stored = {inactive: ["Restore me"], "deleted/patches": ["Keep me"]}
    config["apps"] = [
        {
            "id": PACKAGE,
            "name": "Example",
            "include_patches": stored,
            "exclude_patches": [],
        }
    ]
    save_config(config)

    response = client.get(f"/api/ui/patches/{PACKAGE}?format=grouped")

    assert response.status_code == 200
    groups = {source["repo"]: source for source in response.json()["sources"]}
    assert groups[inactive]["status"] == "inactive"
    assert groups[inactive]["patches"][0]["name"] == "Restore me"
    assert groups["deleted/patches"]["status"] == "deleted"
    assert groups["deleted/patches"]["patches"][0]["name"] == "Keep me"
    assert response.json()["selection"]["stored"] == stored


def test_mapped_save_canonicalizes_repo_and_patch_names_and_preserves_excludes(client):
    repo = "Owner/Patches"
    write_source(repo, [patch("Actual Name")])
    config = make_config()
    config["sources"] = [{"name": "Owner", "repo": repo, "active": True}]
    config["apps"] = [
        {
            "id": PACKAGE,
            "name": "Before",
            "include_patches": ["legacy"],
            "exclude_patches": ["Excluded"],
            "version": "5.0",
            "last_successful_build": {"filename": "old.apk"},
        }
    ]
    save_config(config)

    response = client.post(
        "/api/ui/add_app",
        json={
            "package_id": PACKAGE,
            "name": "After",
            "include_patches": {"owner/patches": ["actual name"]},
        },
    )

    assert response.status_code == 200
    app = config_store.load_config()["apps"][0]
    assert app["include_patches"] == {repo: ["Actual Name"]}
    assert app["exclude_patches"] == ["Excluded"]
    assert app["version"] == "5.0"
    assert app["last_successful_build"] == {"filename": "old.apk"}


def test_mapped_save_is_full_replacement_and_retained_stale_entries_roundtrip(client):
    active, inactive = "active/patches", "inactive/patches"
    write_source(active, [patch("Known")])
    config = make_config()
    config["sources"] = [
        {"name": "Active", "repo": active, "active": True},
        {"name": "Inactive", "repo": inactive, "active": False},
    ]
    config["apps"] = [
        {
            "id": PACKAGE,
            "name": "Example",
            "include_patches": {"deleted/patches": ["Old"]},
            "exclude_patches": [],
        }
    ]
    save_config(config)

    retained = client.post(
        "/api/ui/add_app",
        json={
            "package_id": PACKAGE,
            "name": "Example",
            "include_patches": {
                active: ["Known"],
                inactive: ["Restore"],
                "deleted/patches": ["Old"],
            },
        },
    )
    removed = client.post(
        "/api/ui/add_app",
        json={
            "package_id": PACKAGE,
            "name": "Example",
            "include_patches": {active: ["Known"]},
        },
    )

    assert retained.status_code == 200
    assert removed.status_code == 200
    assert config_store.load_config()["apps"][0]["include_patches"] == {
        active: ["Known"]
    }


@pytest.mark.parametrize(
    "selection",
    [
        {"Owner/Patches": ["Known"], "owner/patches": ["Known"]},
        {"../outside": ["Known"]},
        {"owner/patches": [""]},
        {"owner/patches": "Known"},
    ],
)
def test_mapped_save_rejects_malformed_selection_without_echoing_input(
    client, selection
):
    response = client.post(
        "/api/ui/add_app",
        json={"package_id": PACKAGE, "name": "Example", "include_patches": selection},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid patch selection"
    assert "outside" not in response.text


def test_new_active_unknown_patch_is_rejected_with_fixed_error(client):
    repo = "owner/patches"
    write_source(repo, [patch("Known")])
    config = make_config()
    config["sources"] = [{"name": "Owner", "repo": repo, "active": True}]
    config["apps"] = []
    save_config(config)

    response = client.post(
        "/api/ui/add_app",
        json={
            "package_id": PACKAGE,
            "name": "Example",
            "include_patches": {repo: ["secret-ish"]},
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid patch selection"
    assert "secret-ish" not in response.text
    assert all(app["id"] != PACKAGE for app in config_store.load_config()["apps"])


def test_unchanged_active_stale_selection_can_be_saved_while_list_is_unreadable(client):
    repo = "owner/patches"
    json_path = patcher.get_source_paths(repo)[".json"]
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text("{")
    config = make_config()
    config["sources"] = [{"name": "Owner", "repo": repo, "active": True}]
    config["apps"] = [
        {
            "id": PACKAGE,
            "name": "Example",
            "include_patches": {repo: ["Removed upstream"]},
            "exclude_patches": [],
        }
    ]
    save_config(config)

    response = client.post(
        "/api/ui/add_app",
        json={
            "package_id": PACKAGE,
            "name": "Example",
            "include_patches": {repo: ["Removed upstream"]},
        },
    )

    assert response.status_code == 200
    assert config_store.load_config()["apps"][0]["include_patches"] == {
        repo: ["Removed upstream"]
    }


def test_edit_race_does_not_resurrect_deleted_app(client, monkeypatch):
    config = make_config()
    config["apps"] = [
        {"id": PACKAGE, "name": "Example", "include_patches": [], "exclude_patches": []}
    ]
    save_config(config)
    real_update = main.update_config
    calls = 0

    def delete_then_mutate(mutate):
        nonlocal calls
        calls += 1
        if calls == 1:
            real_update(lambda current: current["apps"].clear())
        return real_update(mutate)

    monkeypatch.setattr(main, "update_config", delete_then_mutate)

    response = client.post(
        "/api/ui/add_app",
        json={"package_id": PACKAGE, "name": "Edited", "include_patches": []},
    )

    assert response.status_code == 404
    assert all(app["id"] != PACKAGE for app in config_store.load_config()["apps"])
