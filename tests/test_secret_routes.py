"""Tests for the secrets routes, response hygiene and CORS."""

import asyncio
import json
import logging

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src import main, secrets_store

FAKE_TOKEN = "fake-token-for-tests-0001"
FAKE_KEY = "fake-licence-for-tests-0002"
GITHUB = "/api/ui/secrets/github-token"
LICENCE = "/api/ui/secrets/cloakbrowser-license-key"
UNSET = {"set": False, "source": None}


@pytest.fixture
def client(isolated_data_dir):
    (isolated_data_dir / "apks" / "icons").mkdir(parents=True)
    return TestClient(main.app)


def test_status_when_nothing_is_set(client):
    response = client.get("/api/ui/secrets")

    assert response.status_code == 200
    assert response.json() == {"github-token": UNSET, "cloakbrowser-license-key": UNSET}


def test_put_then_delete_round_trip(client):
    put = client.put(GITHUB, json={"value": FAKE_TOKEN})

    assert put.status_code == 200
    assert put.json()["github-token"] == {"set": True, "source": "file"}
    assert put.json()["cloakbrowser-license-key"] == UNSET
    assert client.get("/api/ui/secrets").json() == put.json()

    deleted = client.delete(GITHUB)

    assert deleted.status_code == 200
    assert deleted.json() == client.get("/api/ui/secrets").json()
    assert deleted.json()["github-token"] == UNSET


def test_unknown_slug_is_404(client):
    assert (
        client.put("/api/ui/secrets/nope", json={"value": FAKE_TOKEN}).status_code
        == 404
    )
    assert client.delete("/api/ui/secrets/nope").status_code == 404
    assert (
        client.put(
            "/api/ui/secrets/GITHUB_TOKEN", json={"value": FAKE_TOKEN}
        ).status_code
        == 404
    )


def test_environment_secret_cannot_change(client, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-env-value-0006")

    status = client.get("/api/ui/secrets").json()
    put = client.put(GITHUB, json={"value": FAKE_TOKEN})
    deleted = client.delete(GITHUB)

    assert status["github-token"] == {"set": True, "source": "environment"}
    assert put.status_code == 409
    assert deleted.status_code == 409
    assert (
        put.json() == deleted.json() == {"detail": "Secret is set by the environment"}
    )
    assert "fake-env-value-0006" not in put.text + deleted.text + str(status)


@pytest.mark.parametrize(
    "body",
    [
        {"value": "short"},
        {"value": "fake value 0012 with spaces"},
        {"value": "fake-value-0013\nOTHER=injected"},
        {"value": 'fake"value-0014'},
        {"value": 12345678},
        {"value": None},
        {"other": "fake-value-0015"},
        ["fake-value-0016"],
        "fake-value-0017",
        12345678,
    ],
)
def test_invalid_body_is_400_with_fixed_message(client, isolated_data_dir, body):
    response = client.put(GITHUB, json=body)

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid secret value"}
    assert not (isolated_data_dir / ".env").exists()


@pytest.mark.parametrize(
    "raw",
    [b"fake-value-0018 not json", b'{"value": "fake-value-0019"', b"\xff\xfe", b""],
)
def test_malformed_body_does_not_echo_input(client, raw):
    response = client.put(
        GITHUB, content=raw, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid secret value"}


def test_valid_json_over_the_limit_is_400(client, isolated_data_dir):
    body = json.dumps(
        {"value": FAKE_TOKEN, "padding": "a" * main.MAX_SECRET_BODY_BYTES}
    )

    response = client.put(
        GITHUB, content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid secret value"}
    assert not (isolated_data_dir / ".env").exists()


def test_recursion_error_from_the_parser_is_400(client, monkeypatch):
    # The 4 KiB cap keeps real nesting under CPython's limit, so force the
    # error a deeply nested body raises on interpreters with a lower one.
    def too_deep(raw):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(main.json, "loads", too_deep)

    response = client.put(
        GITHUB,
        content="[" * 2000 + "]" * 2000,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.text == '{"detail":"Invalid secret value"}'


class _FakeRequest:
    """Stands in for a Request whose body must not be read."""

    def __init__(self, headers, chunks):
        self.headers = headers
        self.chunks = chunks
        self.consumed = 0

    async def stream(self):
        for chunk in self.chunks:
            self.consumed += len(chunk)
            yield chunk


def test_declared_oversize_is_rejected_before_reading():
    request = _FakeRequest({"content-length": "999999"}, [b"{}"])

    with pytest.raises(HTTPException) as caught:
        asyncio.run(main._read_secret_value(request))

    assert caught.value.status_code == 400
    assert request.consumed == 0


def test_stream_is_cut_off_at_the_limit():
    chunk = b"a" * 1024
    request = _FakeRequest({}, [chunk] * 1000)

    with pytest.raises(HTTPException):
        asyncio.run(main._read_secret_value(request))

    assert request.consumed <= main.MAX_SECRET_BODY_BYTES + 1024


def test_non_numeric_content_length_is_ignored_not_fatal():
    body = json.dumps({"value": FAKE_TOKEN}).encode()
    request = _FakeRequest({"content-length": "abc"}, [body])

    assert asyncio.run(main._read_secret_value(request)) == FAKE_TOKEN


def test_json_body_with_text_plain_type_is_accepted_on_put(client):
    # PUT and DELETE always need a preflight cross-site, so the content type of
    # this route is not a protection; the body is parsed by hand either way.
    response = client.put(
        GITHUB,
        content=json.dumps({"value": FAKE_TOKEN}),
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "text/plain"},
        {"Content-Type": "application/x-www-form-urlencoded"},
        {},
    ],
)
def test_json_post_route_rejects_a_non_json_content_type(client, headers):
    before = main.load_config()["settings"]
    body = {"server_url": "http://evil", "cron_schedule": "", "notification_urls": []}

    response = client.post(
        "/api/ui/save_settings", content=json.dumps(body).encode(), headers=headers
    )

    assert response.status_code == 422
    assert main.load_config()["settings"] == before


def test_read_failure_gives_fixed_500_without_the_value(client, monkeypatch):
    client.put(GITHUB, json={"value": FAKE_TOKEN})

    def broken(*args, **kwargs):
        raise OSError(f"cannot read {FAKE_TOKEN}")

    monkeypatch.setattr(secrets_store, "_read_lines", broken)

    for response in (
        client.get("/api/ui/secrets"),
        client.put(LICENCE, json={"value": FAKE_KEY}),
        client.delete(GITHUB),
    ):
        assert response.status_code == 500
        assert FAKE_TOKEN not in response.text and FAKE_KEY not in response.text
        assert response.json()["detail"].startswith("Could not")


def test_unexpected_failure_does_not_expose_the_value(isolated_data_dir, monkeypatch):
    quiet_client = TestClient(main.app, raise_server_exceptions=False)

    def broken(path, text, mode=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(secrets_store.config_store, "write_file_atomic", broken)

    response = quiet_client.put(GITHUB, json={"value": FAKE_TOKEN})

    assert response.status_code == 500
    assert FAKE_TOKEN not in response.text


def test_no_response_or_log_contains_the_value(client, isolated_data_dir, caplog):
    caplog.set_level(logging.DEBUG)
    responses = [
        client.put(GITHUB, json={"value": FAKE_TOKEN}),
        client.put(LICENCE, json={"value": FAKE_KEY}),
        client.get("/api/ui/secrets"),
        client.put(GITHUB, json={"value": FAKE_TOKEN + "\n"}),
        client.get("/"),
        client.get("/api/apps"),
        client.get("/api/ui/discovery"),
        client.get("/api/ui/logs"),
        client.delete(LICENCE),
    ]

    for response in responses:
        assert FAKE_TOKEN not in response.text
        assert FAKE_KEY not in response.text
        assert FAKE_TOKEN not in str(response.headers)
    assert FAKE_TOKEN not in caplog.text and FAKE_KEY not in caplog.text


def test_value_not_printed(client, capsys):
    client.put(GITHUB, json={"value": FAKE_TOKEN})
    client.get("/")

    captured = capsys.readouterr()
    assert FAKE_TOKEN not in captured.out + captured.err


def test_html_page_does_not_contain_the_value(client):
    client.put(GITHUB, json={"value": FAKE_TOKEN})

    page = client.get("/")

    assert page.status_code == 200
    assert FAKE_TOKEN not in page.text


@pytest.mark.parametrize(
    "path",
    [
        "/download/.env",
        "/download/..%2F.env",
        "/icons/.env",
        "/icons/%2e%2e/%2e%2e/.env",
        "/icons/%2e%2e%2f%2e%2e%2f.env",
        "/.env",
        "/static/.env",
    ],
)
def test_env_file_is_not_served(client, isolated_data_dir, path):
    client.put(GITHUB, json={"value": FAKE_TOKEN})
    assert (isolated_data_dir / ".env").exists()

    response = client.get(path)

    assert response.status_code == 404
    assert FAKE_TOKEN not in response.text


def test_traversal_paths_reach_the_server_unnormalised(client):
    response = client.get("/icons/%2e%2e/%2e%2e/.env")

    assert "%2e%2e" in str(response.request.url)


def test_icons_mount_serves_the_isolated_data_dir(client, isolated_data_dir):
    icons = isolated_data_dir / "apks" / "icons"
    (icons / "fake.png").write_bytes(b"png")

    assert client.get("/icons/fake.png").content == b"png"


def test_logs_route_reads_the_isolated_log(client, isolated_data_dir):
    log = isolated_data_dir / "bin" / "build.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("isolated log line")

    assert client.get("/api/ui/logs").json() == {"logs": "isolated log line"}


def test_os_environ_untouched_by_routes(client, monkeypatch):
    import os

    before = dict(os.environ)

    client.put(GITHUB, json={"value": FAKE_TOKEN})
    client.delete(GITHUB)

    assert dict(os.environ) == before


def test_write_failure_is_500_with_fixed_message(client, monkeypatch):
    def boom(name, value):
        raise OSError("/secret/path " + value)

    monkeypatch.setattr(secrets_store, "set_secret", boom)

    response = client.put(GITHUB, json={"value": FAKE_TOKEN})

    assert response.status_code == 500
    assert response.json() == {"detail": "Could not write the secrets file"}


def test_cross_origin_request_gets_no_allow_origin_header(client):
    simple = client.get("/api/ui/secrets", headers={"Origin": "https://evil.example"})
    preflight = client.options(
        GITHUB,
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert "access-control-allow-origin" not in simple.headers
    assert "access-control-allow-origin" not in preflight.headers
    assert preflight.status_code in (400, 405)


def test_non_browser_client_still_works_without_origin(client):
    assert client.get("/api/apps").status_code == 200
