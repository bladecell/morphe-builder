"""`add_app` rejects an invalid package id and the UI reports a failed save."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src import main
from tests.conftest import make_config, read_config

BAD_IDS = ["com.it's.app", "../../etc/x", "single", "", "com.app name", "com..app"]


@pytest.mark.parametrize("package_id", BAD_IDS)
def test_invalid_package_id_is_rejected_and_nothing_is_written(data_dir, package_id):
    before = read_config(data_dir)
    payload = main.AddAppPayload(package_id=package_id, name="X", include_patches=[])

    with pytest.raises(HTTPException) as caught:
        main.add_app_to_config(payload)

    assert caught.value.status_code == 400
    assert caught.value.detail == "Invalid package id"
    assert read_config(data_dir) == before


def test_route_answers_400_like_the_upload_route(data_dir):
    client = TestClient(main.app)

    response = client.post(
        "/api/ui/add_app",
        json={"package_id": "com.it's.app", "name": "X", "include_patches": []},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid package id"}


def test_valid_package_id_is_still_saved(data_dir):
    payload = main.AddAppPayload(package_id="com.new.app", name="N", include_patches=[])

    main.add_app_to_config(payload)

    ids = [a["id"] for a in read_config(data_dir)["apps"]]
    assert "com.new.app" in ids


def test_editing_an_existing_valid_app_still_works(data_dir):
    existing = make_config()["apps"][0]
    payload = main.AddAppPayload(
        package_id=existing["id"], name="Renamed", include_patches=["two"]
    )

    main.add_app_to_config(payload)

    app = next(a for a in read_config(data_dir)["apps"] if a["id"] == existing["id"])
    assert app["name"] == "Renamed"


def test_delete_app_still_removes_an_id_that_would_now_be_invalid(data_dir):
    def add_bad_id(config: dict) -> None:
        config["apps"].append({"id": "com.it's.app", "name": "Bad"})

    main.update_config(add_bad_id)

    main.delete_app_from_config(main.DeleteAppPayload(package_id="com.it's.app"))

    assert "com.it's.app" not in [a["id"] for a in read_config(data_dir)["apps"]]


def render_script() -> str:
    template = main.templates.get_template("index.html")
    return template.render(
        config=make_config(), available_apps=[], metadata={}, request=None
    )


def test_save_app_checks_the_response_before_reloading():
    html = render_script()
    start = html.index("async saveApp()")
    body = html[start : html.index("async uploadApk()", start)]

    assert "response.ok" in body
    assert body.index("response.ok") < body.index("location.reload()")
