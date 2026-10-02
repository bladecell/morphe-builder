"""HTTP-level tests for upload, download, Obtainium lookups, settings and the UI card."""

import os
import re
import threading
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from src import main
from tests.conftest import make_config, read_config


@pytest.fixture
def client(data_dir) -> TestClient:
    return TestClient(main.app)


def raw_files(data_dir: Path) -> list[Path]:
    raw_dir = data_dir / "apks" / "raw"
    return sorted(raw_dir.rglob("*")) if raw_dir.exists() else []


def post_upload(client: TestClient, package_id: str, version: str):
    return client.post(
        "/api/ui/upload_apk",
        data={"package_id": package_id, "version_str": version},
        files={"file": ("a.apk", b"apk-bytes")},
    )


# --- 1. upload path safety -------------------------------------------------


@pytest.mark.parametrize(
    "package_id, version",
    [
        ("../../evil", "1.0"),
        ("com.foo/../../x", "1.0"),
        ("com/foo", "1.0"),
        ("com.foo\\bar", "1.0"),
        ("", "1.0"),
        ("nodots", "1.0"),
        ("com.foo bar", "1.0"),
        ("com.foo", "../../evil"),
        ("com.foo", "1.0/../../x"),
        ("com.foo", "1..0"),
        ("com.foo", "a\\b"),
        ("com.foo", ""),
        ("com.foo", "   "),
        ("com.foo", "x" * 200),
    ],
)
def test_upload_rejects_unsafe_names_and_writes_nothing(
    client, data_dir, package_id, version
):
    response = post_upload(client, package_id, version)

    assert response.status_code == 400
    assert raw_files(data_dir) == []
    assert not (data_dir.parent / "evil.apk").exists()


def test_upload_accepts_normal_names(client, data_dir, monkeypatch):
    monkeypatch.setattr("src.patcher.extract_app_metadata", lambda *a, **k: None)

    response = post_upload(client, "com.google.android.youtube", "19.16 beta.1")

    assert response.status_code == 200
    names = [p.name for p in raw_files(data_dir)]
    assert names == ["com.google.android.youtube-19.16_beta.1.apk"]


# --- 4. cron schedule follows the file -------------------------------------


def test_concurrent_saves_leave_scheduler_matching_file(data_dir, monkeypatch):
    scheduled: list[str] = []
    first_call_entered = threading.Event()
    release_first = threading.Event()

    def fake_setup(config: dict) -> None:
        cron = config["settings"]["cron_schedule"]
        is_first = not first_call_entered.is_set()
        first_call_entered.set()
        if is_first:
            release_first.wait(timeout=5)
        scheduled.append(cron)

    monkeypatch.setattr(main, "setup_cron_job", fake_setup)

    def save(cron: str) -> None:
        main.save_settings(
            main.SaveSettingsPayload(server_url="http://x", cron_schedule=cron)
        )

    thread_a = threading.Thread(target=save, args=("0 1 * * *",))
    thread_b = threading.Thread(target=save, args=("0 2 * * *",))
    thread_a.start()
    assert first_call_entered.wait(timeout=5)
    thread_b.start()
    time.sleep(0.3)
    release_first.set()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)

    on_disk = read_config(data_dir)["settings"]["cron_schedule"]
    assert on_disk == "0 2 * * *"
    assert scheduled[-1] == on_disk


# --- 5. /download exact package match --------------------------------------


@pytest.fixture
def patched_dir(data_dir) -> Path:
    directory = data_dir / "apks" / "patched"
    directory.mkdir(parents=True)
    return directory


def make_apk(directory: Path, name: str, mtime: float) -> None:
    path = directory / name
    path.write_bytes(name.encode())
    os.utime(path, (mtime, mtime))


def test_download_does_not_serve_longer_package_id(client, patched_dir):
    make_apk(patched_dir, "com.foobar-1.0-v1-abcd1234.apk", 1000)

    assert client.get("/download/com.foo").status_code == 404


def test_download_serves_newest_of_exact_package(client, patched_dir):
    make_apk(patched_dir, "com.foo-1.0-v1-aaaaaaaa.apk", 1000)
    make_apk(patched_dir, "com.foo-1.1-v1-bbbbbbbb.apk", 2000)
    make_apk(patched_dir, "com.foobar-9.9-v1-cccccccc.apk", 3000)

    response = client.get("/download/com.foo")

    assert response.status_code == 200
    assert response.content == b"com.foo-1.1-v1-bbbbbbbb.apk"


def test_download_supports_legacy_forms_and_old_names(client, patched_dir):
    make_apk(patched_dir, "com.foo-1.0-v1.apk", 1000)

    for request_name in ("com.foo-patched.apk", "com.foo.apk", "com.foo"):
        response = client.get(f"/download/{request_name}")
        assert response.status_code == 200, request_name
        assert response.content == b"com.foo-1.0-v1.apk"


def test_download_exact_filename_and_unknown(client, patched_dir):
    make_apk(patched_dir, "com.foo-1.0-v1-aaaaaaaa.apk", 1000)

    exact = client.get("/download/com.foo-1.0-v1-aaaaaaaa.apk")

    assert exact.status_code == 200
    assert client.get("/download/com.nothing").status_code == 404


def test_download_treats_glob_characters_literally(client, patched_dir):
    make_apk(patched_dir, "com.foo-1.0-v1-aaaaaaaa.apk", 1000)

    assert client.get("/download/com.f*").status_code == 404


# --- 6. Obtainium lookups ---------------------------------------------------


@pytest.fixture
def obtainium_config(data_dir) -> None:
    config = make_config()
    config["apps"] = [
        {"id": "com.hand", "name": "Hand written"},
        {"id": "com.a", "name": "A", "obtainium_id": "morphe_a"},
    ]
    (data_dir / "config.yaml").write_text(yaml.dump(config, sort_keys=False))


def test_api_apps_unknown_id_is_404(client, obtainium_config):
    assert client.get("/api/apps/morphe_nope").status_code == 404


def test_api_apps_lookup_survives_entry_without_obtainium_id(client, obtainium_config):
    response = client.get("/api/apps/morphe_a")

    assert response.status_code == 200
    assert response.json()["id"] == "com.a"


def test_view_survives_entry_without_obtainium_id(client, obtainium_config):
    by_obtainium_id = client.get("/view/morphe_a")
    unknown = client.get("/view/morphe_nope")

    assert by_obtainium_id.status_code == 200
    assert unknown.status_code == 404


def test_api_apps_lists_entry_without_obtainium_id(client, obtainium_config):
    response = client.get("/api/apps")

    assert response.status_code == 200
    assert {a["id"] for a in response.json()["apps"]} == {"com.hand", "com.a"}


# --- 3. pipeline card renders names safely ---------------------------------


def render_index(app_name: str) -> str:
    config = make_config()
    config["apps"] = [{"id": "com.a", "name": app_name, "include_patches": []}]
    template = main.templates.get_template("index.html")
    return template.render(config=config, available_apps=[], metadata={}, request=None)


def test_card_does_not_interpolate_name_into_attributes():
    name = 'Bob\'s "App" <b>'

    html = render_index(name)

    assert "<b>" not in html
    assert "Bob&#39;s" not in html
    assert "&lt;b&gt;" not in html
    assert re.search(r"confirm\([^)]*pipelineApps\[0\]\.name", html)
    assert "Bob\\u0027s" in html  # present only as JSON data
