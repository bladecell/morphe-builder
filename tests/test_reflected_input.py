"""Error answers never echo request input, so masking cannot confirm a secret."""

import random
import string

import pytest
from fastapi.testclient import TestClient

from src import main, secrets_store

# Made only of characters that are valid in a repository name and a file name.
SECRET = "stored-secret-value-0042"


def same_length_random(text: str) -> str:
    rng = random.Random(7)
    return "".join(rng.choice(string.ascii_lowercase) for _ in text)


@pytest.fixture
def client(isolated_data_dir, monkeypatch):
    (isolated_data_dir / "apks" / "icons").mkdir(parents=True)
    secrets_store.set_secret(secrets_store.CLOAKBROWSER_LICENSE_KEY, SECRET)
    monkeypatch.setattr(main, "check_and_download_release", lambda *a, **k: False)
    return TestClient(main.app)


def test_download_404_answers_the_same_for_a_secret_and_a_random_name(client):
    secret = client.get(f"/download/{SECRET}")
    other = client.get(f"/download/{same_length_random(SECRET)}")

    assert secret.status_code == other.status_code == 404
    assert secret.content == other.content


def test_download_404_does_not_echo_the_requested_name(client):
    response = client.get("/download/some-name-0123.apk")

    assert "some-name-0123" not in response.text


def test_add_source_missing_files_answer_the_same_for_a_secret_and_a_random_name(
    client,
):
    secret = client.post("/api/ui/add_source", json={"repo": f"own/{SECRET}"})
    other = client.post(
        "/api/ui/add_source", json={"repo": f"own/{same_length_random(SECRET)}"}
    )

    assert secret.status_code == other.status_code == 502
    assert secret.content == other.content


def test_add_source_mpp_missing_answers_without_the_repo_name(
    client, monkeypatch, isolated_data_dir
):
    def write_json_only(repo, paths, force=False):
        paths[".json"].parent.mkdir(parents=True, exist_ok=True)
        paths[".json"].write_text("[]")
        return True

    monkeypatch.setattr(main, "check_and_download_release", write_json_only)

    secret = client.post("/api/ui/add_source", json={"repo": f"own/{SECRET}"})
    other = client.post(
        "/api/ui/add_source", json={"repo": f"own/{same_length_random(SECRET)}"}
    )

    assert secret.status_code == other.status_code == 502
    assert secret.content == other.content
    assert SECRET not in secret.text


def test_duplicate_source_answer_does_not_echo_the_repo(client):
    client.post("/api/ui/add_source", json={"repo": "own/dup-source"})
    main.update_config(
        lambda c: c.setdefault("sources", []).append(
            {"name": "d", "repo": "own/dup-source", "active": True}
        )
    )

    response = client.post("/api/ui/add_source", json={"repo": "own/dup-source"})

    assert response.status_code == 400
    assert "dup-source" not in response.text
