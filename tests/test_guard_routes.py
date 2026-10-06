"""The guards on the real app: Origin check, Host allow-list, discovery sync."""

import asyncio
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import config_store, main, patcher, request_guards

FAKE_TOKEN = "fake-token-for-tests-0001"
SECRET = "/api/ui/secrets/github-token"
SETTINGS = {"server_url": "http://x", "cron_schedule": "", "notification_urls": []}
TEMPLATE = Path(main.__file__).parent / "templates" / "index.html"


@pytest.fixture
def client(data_dir):
    (data_dir / "apks" / "icons").mkdir(parents=True)
    return TestClient(main.app)


@pytest.fixture
def no_allowed_hosts(monkeypatch):
    """Run with `ALLOWED_HOSTS` unset: only then does the server URL count."""
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)


def set_server_url(url):
    config_store.update_config(lambda config: config["settings"].update(server_url=url))


def call_asgi(headers, method="GET", path="/api/ui/build_status"):
    """Run the app on a raw ASGI scope so the headers are exactly as given."""
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.encode(), v.encode()) for k, v in headers],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }
    asyncio.run(main.app(scope, receive, send))
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages)
    return status, body.decode()


# --- Origin ---


@pytest.mark.parametrize(
    "request_args",
    [
        ("post", "/api/ui/save_settings", {"json": SETTINGS}),
        ("put", SECRET, {"json": {"value": FAKE_TOKEN}}),
        ("delete", SECRET, {}),
        ("post", "/api/ui/trigger_build", {"json": {"package_ids": None}}),
        ("post", "/api/ui/delete_app", {"json": {"package_id": "com.a"}}),
        ("patch", "/api/ui/save_settings", {"json": SETTINGS}),
    ],
)
@pytest.mark.parametrize(
    "origin",
    ["https://evil.example", "http://testserver:9999", "http://x.evil", "null"],
)
def test_cross_origin_state_change_is_refused(client, data_dir, request_args, origin):
    method, path, kwargs = request_args
    before = (data_dir / "config.yaml").read_text()

    response = getattr(client, method)(path, headers={"Origin": origin}, **kwargs)

    assert response.status_code == 403
    assert response.json() == {"detail": request_guards.ORIGIN_REFUSED}
    assert (data_dir / "config.yaml").read_text() == before
    assert not (data_dir / ".env").exists()


def test_multipart_upload_from_another_origin_is_refused(client, data_dir):
    response = client.post(
        "/api/ui/upload_apk",
        data={"package_id": "com.a", "version_str": "1.0"},
        files={"file": ("a.apk", b"fake apk", "application/octet-stream")},
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert not (data_dir / "apks" / "raw").exists()


def test_multipart_upload_from_the_same_origin_works(client, data_dir, monkeypatch):
    monkeypatch.setattr(patcher, "extract_app_metadata", lambda *a, **k: None)
    response = client.post(
        "/api/ui/upload_apk",
        data={"package_id": "com.a", "version_str": "1.0"},
        files={"file": ("a.apk", b"fake apk", "application/octet-stream")},
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 200
    assert (data_dir / "apks" / "raw" / "com.a-1.0.apk").exists()


def test_same_origin_and_no_origin_are_allowed(client):
    same = client.put(
        SECRET, json={"value": FAKE_TOKEN}, headers={"Origin": "http://testserver"}
    )
    none = client.delete(SECRET)

    assert same.status_code == 200
    assert none.status_code == 200


@pytest.mark.usefixtures("no_allowed_hosts")
def test_origin_matching_server_url_is_allowed_when_host_differs(client, data_dir):
    set_server_url("https://morphe.example.com")

    host = {"Host": "10.0.0.2:8000"}
    ok = client.delete(SECRET, headers={**host, "Origin": "https://morphe.example.com"})
    bad = client.delete(SECRET, headers={**host, "Origin": "https://morphe.example.org"})

    assert ok.status_code == 200
    assert bad.status_code == 403


def test_origin_not_checked_for_reads(client):
    response = client.get("/api/ui/build_status", headers={"Origin": "https://evil"})

    assert response.status_code == 200


def test_options_reaches_the_app_and_gets_no_cors_grant(client):
    response = client.options("/api/ui/save_settings", headers={"Origin": "https://e"})

    # 405 comes from the route table, so the guard let it through; no CORS headers.
    assert response.status_code == 405
    assert "access-control-allow-origin" not in response.headers


def test_two_origin_headers_are_refused():
    status, _ = call_asgi(
        [
            ("host", "testserver"),
            ("origin", "http://testserver"),
            ("origin", "http://testserver"),
        ],
        method="DELETE",
        path=SECRET,
    )

    assert status == 403


def test_x_forwarded_host_is_ignored(client):
    response = client.delete(
        SECRET,
        headers={"X-Forwarded-Host": "evil.example", "Origin": "http://evil.example"},
    )

    assert response.status_code == 403


# --- Host ---


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.1:8000",
        "[::1]",
        "[::1]:8000",
        "localhost",
        "LOCALHOST:8000",
        "localhost.",
        "testserver",
    ],
)
def test_allowed_host_is_served(client, host):
    response = client.get("/api/ui/build_status", headers={"Host": host})

    assert response.status_code == 200


@pytest.mark.usefixtures("no_allowed_hosts")
def test_server_url_host_is_served(client, data_dir):
    set_server_url("https://morphe.example.com")

    ok = client.get("/api/ui/build_status", headers={"Host": "Morphe.Example.com:443"})
    other = client.get("/api/ui/build_status", headers={"Host": "other.example.com"})

    assert ok.status_code == 200
    assert other.status_code == 400


def test_full_origin_entry_in_allowed_hosts_is_served_as_a_host(client, monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "testserver,https://morphe.example.com:8443")

    response = client.get(
        "/api/ui/build_status", headers={"Host": "morphe.example.com"}
    )

    assert response.status_code == 200


def test_allowed_hosts_env_name_is_served(client, monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "testserver, nas.lan")

    response = client.get("/api/ui/build_status", headers={"Host": "nas.lan:8000"})

    assert response.status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:8000",
        "user@localhost",
        "localhost/x",
        "::1",
        "127.0.0.1.evil.example",
        "<script>alert(1)</script>.evil",
    ],
)
@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/api/ui/build_status",
        "/api/apps",
        "/download/com.a",
        "/view/com.a",
        "/icons/x.png",
        "/nope",
    ],
)
def test_unknown_host_is_refused_everywhere(client, host, path):
    response = client.get(path, headers={"Host": host})

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert "ALLOWED_HOSTS" in response.json()["detail"]
    assert "server URL" in response.json()["detail"]
    assert host not in response.text
    assert "<" not in response.text


def test_unknown_host_is_refused_for_writes_too(client):
    response = client.delete(SECRET, headers={"Host": "evil.example"})

    assert response.status_code == 400


def test_missing_host_is_refused():
    status, body = call_asgi([])

    assert status == 400
    assert "ALLOWED_HOSTS" in body


def test_two_host_headers_are_refused():
    status, _ = call_asgi([("host", "localhost"), ("host", "evil.example")])

    assert status == 400


@pytest.mark.parametrize(
    "content", [": : not yaml [", "- a\n- b\n", "settings: 5\n", ""]
)
def test_unreadable_config_does_not_fail_the_request(client, data_dir, content):
    (data_dir / "config.yaml").write_text(content)

    ip = client.get("/api/ui/build_status", headers={"Host": "127.0.0.1"})
    name = client.get("/api/ui/build_status", headers={"Host": "morphe.example.com"})

    assert ip.status_code == 200
    assert name.status_code == 400


def test_missing_config_is_not_created_by_the_guard(isolated_data_dir):
    client = TestClient(main.app)

    response = client.get("/api/ui/build_status", headers={"Host": "127.0.0.1"})

    assert response.status_code == 200
    assert not (isolated_data_dir / "config.yaml").exists()


@pytest.mark.usefixtures("no_allowed_hosts")
def test_config_change_is_picked_up(client, data_dir):
    first = client.get("/api/ui/build_status", headers={"Host": "morphe.example.com"})
    set_server_url("https://morphe.example.com")
    second = client.get("/api/ui/build_status", headers={"Host": "morphe.example.com"})

    assert (first.status_code, second.status_code) == (400, 200)


def test_obtainium_paths_work_for_an_allowed_host_without_origin(client, data_dir):
    patched = data_dir / "apks" / "patched"
    patched.mkdir(parents=True)
    (patched / "com.a-1.0.apk").write_bytes(b"apk")

    apps = client.get("/api/apps", headers={"Host": "192.168.1.5:8000"})
    download = client.get("/download/com.a", headers={"Host": "192.168.1.5:8000"})
    view = client.get("/view/com.a", headers={"Host": "192.168.1.5:8000"})

    assert apps.status_code == 200
    assert download.status_code == 200 and download.content == b"apk"
    assert view.status_code == 200


def test_lifespan_scope_passes_through(data_dir, monkeypatch):
    events: list[str] = []

    class FakeWorker:
        def start(self):
            events.append("worker start")

        def shutdown(self):
            events.append("worker shutdown")

        def is_alive(self):
            return False

    class FakeScheduler:
        running = True

        def start(self):
            events.append("scheduler start")

        def add_job(self, *args, **kwargs):
            events.append("add job")

        def shutdown(self, wait=True):
            events.append("scheduler shutdown")

    monkeypatch.setattr(main, "SCRAPER_WORKER", FakeWorker())
    monkeypatch.setattr(main, "scheduler", FakeScheduler())
    monkeypatch.setattr(main, "update_tools_task", lambda *a, **k: None)
    monkeypatch.setattr(main, "setup_cron_job", lambda config: None)
    monkeypatch.setattr(main, "check_dependencies", lambda config: {})

    with TestClient(main.app):
        assert "worker start" in events and "scheduler start" in events

    assert "worker shutdown" in events and "scheduler shutdown" in events


# --- Discovery sync ---


@pytest.fixture
def sync_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "update_tools_task", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(main, "get_all_available_apps", lambda config: [{"pkg": "p"}])
    return calls


def test_get_never_syncs(client, sync_calls):
    for query in ("?sync=true", "?sync=1", ""):
        response = client.get(f"/api/ui/discovery{query}")
        assert response.json() == {"apps": [{"pkg": "p"}]}

    assert sync_calls == []


def test_post_syncs_and_returns_the_apps(client, sync_calls):
    response = client.post("/api/ui/discovery/sync")

    assert response.status_code == 200
    assert response.json() == {"apps": [{"pkg": "p"}]}
    assert len(sync_calls) == 1


def test_cross_origin_sync_is_refused(client, sync_calls):
    response = client.post(
        "/api/ui/discovery/sync", headers={"Origin": "https://evil.example"}
    )

    assert response.status_code == 403
    assert sync_calls == []


def test_template_calls_sync_by_post_only():
    html = TEMPLATE.read_text()

    assert "sync=true" not in html
    assert "/api/ui/discovery/sync" in html
    assert "method: 'POST'" in html.split("async refreshDiscovery")[1][:400]


# --- Review follow-up: ALLOWED_HOSTS unset, forwarded host, proxy, loopback ---


def test_nothing_but_ip_and_localhost_is_served_without_allowed_hosts(
    client, monkeypatch
):
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)

    refused = [
        client.get("/api/ui/build_status", headers={"Host": h}).status_code
        for h in ("testserver", "nas.lan", "morphe.example.com:8000")
    ]
    served = [
        client.get("/api/ui/build_status", headers={"Host": h}).status_code
        for h in ("127.0.0.1:8000", "localhost", "[::1]:8000", "192.168.1.5")
    ]

    assert refused == [400, 400, 400]
    assert served == [200, 200, 200, 200]


def test_forwarded_host_cannot_make_an_unknown_host_pass(client):
    response = client.get(
        "/api/ui/build_status",
        headers={"Host": "evil", "X-Forwarded-Host": "localhost"},
    )

    assert response.status_code == 400


def test_refusal_messages_tell_the_operator_what_to_set(client):
    response = client.delete(SECRET, headers={"Origin": "https://evil.example"})

    assert response.status_code == 403
    assert "full origin" in response.json()["detail"]
    assert "https://morphe.example.com" in response.json()["detail"]
    assert "ALLOWED_HOSTS" in response.json()["detail"]
    assert "server URL" in response.json()["detail"]
    assert "evil.example" not in response.text


def test_proxy_that_rewrites_host_to_an_ip_can_be_fixed_by_allowed_hosts(
    client, monkeypatch
):
    public = {"Host": "10.0.0.2:8000", "Origin": "https://morphe.example.com"}
    locked_out = client.post("/api/ui/save_settings", json=SETTINGS, headers=public)
    monkeypatch.setenv("ALLOWED_HOSTS", "testserver,https://morphe.example.com")

    fixed = client.post("/api/ui/save_settings", json=SETTINGS, headers=public)
    wrong_port = client.post(
        "/api/ui/save_settings",
        json=SETTINGS,
        headers={**public, "Origin": "https://morphe.example.com:8443"},
    )
    other_name = client.post(
        "/api/ui/save_settings",
        json=SETTINGS,
        headers={**public, "Origin": "https://evil.example"},
    )

    assert locked_out.status_code == 403
    assert fixed.status_code == 200
    assert wrong_port.status_code == 403
    assert other_name.status_code == 403
    monkeypatch.setenv("ALLOWED_HOSTS", "testserver,morphe.example.com")
    bare = client.post("/api/ui/save_settings", json=SETTINGS, headers=public)
    assert bare.status_code == 403  # a bare name is a Host only


@pytest.mark.usefixtures("no_allowed_hosts")
def test_proxy_case_also_works_through_the_server_url(client):
    set_server_url("https://morphe.example.com")
    headers = {"Host": "10.0.0.2:8000", "Origin": "https://morphe.example.com"}

    assert client.delete(SECRET, headers=headers).status_code == 200


@pytest.mark.usefixtures("no_allowed_hosts")
def test_loopback_server_url_is_not_an_origin_for_a_remote_host(client):
    set_server_url("http://localhost:8000")
    origin = {"Origin": "http://localhost:8000"}

    remote = client.delete(SECRET, headers={**origin, "Host": "192.168.1.5:8000"})
    local = client.delete(SECRET, headers={**origin, "Host": "127.0.0.1:8000"})

    assert remote.status_code == 403
    assert local.status_code == 200


@pytest.mark.usefixtures("no_allowed_hosts")
def test_same_host_proxy_that_rewrites_host_to_loopback_is_trusted(client):
    # Known and accepted: a proxy on this machine that rewrites Host to loopback
    # makes the request look local, so the default server URL vouches for the
    # browser's Origin. The proxy must pass the original Host through, or the
    # loopback and rebinding rules do not hold (see the request_guards docstring).
    set_server_url("http://localhost:8000")

    response = client.delete(
        SECRET, headers={"Host": "127.0.0.1:8000", "Origin": "http://localhost:8000"}
    )

    assert response.status_code == 200


def test_default_loopback_server_url_stays_in_the_config(client):
    set_server_url("http://localhost:8000")

    assert main.load_config()["settings"]["server_url"] == "http://localhost:8000"


# --- Framing headers ---


def assert_not_frameable(response):
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert response.headers["x-frame-options"] == "DENY"


def test_every_kind_of_response_refuses_framing(client, data_dir):
    (data_dir / "apks" / "icons" / "a.png").write_bytes(b"png")
    responses = [
        client.get("/"),
        client.get("/api/ui/build_status"),
        client.get("/nope"),
        client.get("/icons/a.png"),
        client.get("/icons/missing.png"),
        client.get("/download/missing"),
        client.get("/view/com.a"),
        client.delete(SECRET),
        client.get("/api/ui/build_status", headers={"Host": "evil.example"}),
        client.delete(SECRET, headers={"Origin": "https://evil.example"}),
        client.post("/api/ui/save_settings", content=b"x"),
    ]

    for response in responses:
        assert_not_frameable(response)
    assert [r.status_code for r in responses][-3:-1] == [400, 403]


def test_missing_host_refusal_refuses_framing_too():
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
    }
    asyncio.run(main.app(scope, receive, send))

    start = next(m for m in messages if m["type"] == "http.response.start")
    headers = {k: v for k, v in start["headers"]}
    assert start["status"] == 400
    assert headers[b"x-frame-options"] == b"DENY"


# --- Config warning is not logged on every request ---


def test_unreadable_config_warns_once_and_still_fails_closed(client, data_dir, caplog):
    (data_dir / "config.yaml").write_text(": : not yaml [")
    caplog.set_level(logging.WARNING, logger="src.request_guards")

    codes = [
        client.get("/api/ui/build_status", headers={"Host": "morphe.example.com"})
        for _ in range(5)
    ]

    assert {r.status_code for r in codes} == {400}
    warnings = [r for r in caplog.records if r.name == "src.request_guards"]
    assert len(warnings) == 1


@pytest.mark.usefixtures("no_allowed_hosts")
def test_config_fixed_after_a_warning_is_read_again(client, data_dir, caplog):
    (data_dir / "config.yaml").write_text(": : not yaml [")
    broken = client.get("/api/ui/build_status", headers={"Host": "morphe.example.com"})
    (data_dir / "config.yaml").write_text(
        "settings:\n  server_url: https://morphe.example.com/\n"
    )

    fixed = client.get("/api/ui/build_status", headers={"Host": "morphe.example.com"})

    assert (broken.status_code, fixed.status_code) == (400, 200)


def test_missing_config_warns_once(isolated_data_dir, caplog):
    client = TestClient(main.app)
    caplog.set_level(logging.WARNING, logger="src.request_guards")

    for _ in range(4):
        client.get("/api/ui/build_status", headers={"Host": "127.0.0.1"})

    assert len([r for r in caplog.records if r.name == "src.request_guards"]) == 1


def test_save_settings_shows_the_servers_error():
    html = TEMPLATE.read_text()
    body = html.split("async saveSettings")[1].split("async ")[0]

    assert "response.ok" in body
    assert "detail" in body
    assert "Settings saved!" in body


# --- Framing: more response kinds, an inner response with its own headers ---------


def test_successful_download_and_redirect_refuse_framing(client, data_dir):
    patched = data_dir / "apks" / "patched"
    patched.mkdir(parents=True)
    (patched / "com.a-1.0.apk").write_bytes(b"apk")

    download = client.get("/download/com.a")
    redirect = client.get("/api/apps/", follow_redirects=False)

    assert download.status_code == 200 and download.content == b"apk"
    assert redirect.status_code in (301, 307)
    assert_not_frameable(download)
    assert_not_frameable(redirect)


def run_inner(inner):
    """Run `inner` behind the guard and return (status, headers list)."""
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"host", b"127.0.0.1")],
        "query_string": b"",
    }
    asyncio.run(request_guards.RequestGuardMiddleware(inner)(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    return start["status"], start["headers"]


def test_inner_framing_headers_cannot_weaken_ours():
    async def inner(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"X-Frame-Options", b"SAMEORIGIN"),
                    (b"x-frame-options", b"ALLOWALL"),
                    (b"Content-Security-Policy", b"frame-ancestors *"),
                    (b"content-security-policy", b"default-src 'self'"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    _, headers = run_inner(inner)

    names = [k.lower() for k, _ in headers]
    assert names.count(b"x-frame-options") == 1
    assert (b"x-frame-options", b"DENY") in [(k.lower(), v) for k, v in headers]
    csp = [v for k, v in headers if k.lower() == b"content-security-policy"]
    # Every CSP header is enforced, so the app's own policy stays and ours is added.
    assert b"default-src 'self'" in csp
    assert b"frame-ancestors 'none'" in csp


# --- Unhandled errors: fixed 500, framing headers, logged without the detail -------


def test_unhandled_error_is_a_fixed_500_with_framing_headers_and_is_logged(
    client, monkeypatch, caplog
):
    quiet = TestClient(main.app, raise_server_exceptions=False)
    client.put(SECRET, json={"value": FAKE_TOKEN})

    def boom():
        raise RuntimeError(f"trace detail {FAKE_TOKEN}")

    monkeypatch.setattr(main, "load_config", boom)
    caplog.set_level(logging.ERROR)

    response = quiet.get("/api/apps")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}
    assert "trace detail" not in response.text and FAKE_TOKEN not in response.text
    assert_not_frameable(response)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors
    assert "RuntimeError" in caplog.text
    assert FAKE_TOKEN not in caplog.text


def test_error_after_the_response_started_is_not_swallowed():
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("late")

    with pytest.raises(RuntimeError):
        run_inner(inner)
