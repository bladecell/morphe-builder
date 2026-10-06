"""Loopback port rule, Sec-Fetch-Site, and the server URL as a trust anchor."""

import pytest
from fastapi.testclient import TestClient

from src import config_store, main, request_guards
from tests.test_guard_routes import SECRET, SETTINGS, call_asgi, set_server_url

LOCAL = "http://localhost:8000"


@pytest.fixture
def client(data_dir):
    (data_dir / "apks" / "icons").mkdir(parents=True)
    return TestClient(main.app)


@pytest.fixture
def no_allowed_hosts(monkeypatch):
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)


# --- item 6: a loopback server URL vouches only for the port the app is on ---------


@pytest.mark.parametrize(
    ("origin", "host", "allowed"),
    [
        (LOCAL, "localhost:8000", True),
        (LOCAL, "127.0.0.1:8000", True),
        (LOCAL, "[::1]:8000", True),
        (LOCAL, "localhost:9000", False),
        (LOCAL, "127.0.0.1:9000", False),
        (LOCAL, "localhost", False),
        ("http://localhost", "127.0.0.1:80", True),
        ("http://localhost", "127.0.0.1", True),
        ("http://localhost", "127.0.0.1:8000", False),
    ],
)
def test_loopback_server_url_needs_the_port_of_the_host_header(origin, host, allowed):
    server_url = origin

    assert request_guards.is_origin_allowed(origin, host, server_url) is allowed


def test_loopback_origin_of_the_default_url_is_refused_on_another_port(
    client, no_allowed_hosts
):
    set_server_url(LOCAL)

    other_port = client.delete(
        SECRET, headers={"Host": "localhost:9000", "Origin": LOCAL}
    )
    same_port = client.delete(
        SECRET, headers={"Host": "localhost:8000", "Origin": LOCAL}
    )

    assert other_port.status_code == 403
    assert same_port.status_code == 200


def test_proxy_origin_through_a_public_server_url_still_passes(
    client, no_allowed_hosts
):
    set_server_url("https://morphe.example.com")
    headers = {"Host": "10.0.0.2:8000", "Origin": "https://morphe.example.com"}

    assert client.delete(SECRET, headers=headers).status_code == 200


# --- item 7: Sec-Fetch-Site: cross-site ------------------------------------------------

STATE_CHANGING = [
    ("POST", "/api/ui/save_settings"),
    ("PUT", SECRET),
    ("DELETE", SECRET),
    ("PATCH", "/api/ui/save_settings"),
]


@pytest.mark.parametrize(("method", "path"), STATE_CHANGING)
def test_cross_site_state_change_without_origin_is_refused(
    client, data_dir, method, path
):
    before = (data_dir / "config.yaml").read_text()

    response = client.request(
        method, path, headers={"Sec-Fetch-Site": "cross-site"}, json=SETTINGS
    )

    assert response.status_code == 403
    assert "cross-site" in response.json()["detail"].lower()
    assert (data_dir / "config.yaml").read_text() == before


@pytest.mark.parametrize("value", ["same-origin", "none", "same-site"])
def test_other_fetch_sites_pass_for_state_changes_without_origin(client, value):
    response = client.delete(SECRET, headers={"Sec-Fetch-Site": value})

    assert response.status_code == 200


def test_state_change_without_the_header_is_unaffected(client):
    assert client.delete(SECRET).status_code == 200


def test_same_site_sibling_origin_is_still_refused_by_the_origin_rule(client):
    response = client.delete(
        SECRET,
        headers={"Sec-Fetch-Site": "same-site", "Origin": "https://sibling.example"},
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    "path", ["/api/ui/updates", "/api/ui/build_status", "/api/ui/discovery"]
)
def test_cross_site_get_of_the_ui_api_is_refused(client, path):
    response = client.get(path, headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 403


@pytest.mark.parametrize(
    "path", ["/view/com.a", "/download/com.a", "/api/apps", "/api/apps/x", "/"]
)
@pytest.mark.parametrize("mode", ["navigate", "cors", "no-cors"])
def test_cross_site_get_of_obtainium_pages_still_works(client, path, mode):
    response = client.get(
        path, headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": mode}
    )

    assert response.status_code != 403


def test_cross_site_get_of_the_ui_api_is_allowed_without_the_header(client):
    assert client.get("/api/ui/build_status").status_code == 200


def test_a_repeated_header_fails_closed_on_a_state_change():
    status, _ = call_asgi(
        [
            ("host", "localhost"),
            ("sec-fetch-site", "same-origin"),
            ("sec-fetch-site", "cross-site"),
        ],
        method="DELETE",
        path=SECRET,
    )

    assert status == 403


def test_an_unknown_value_fails_closed_on_a_state_change(client):
    response = client.delete(SECRET, headers={"Sec-Fetch-Site": "sideways"})

    assert response.status_code == 403


def test_an_unknown_value_on_a_plain_get_passes(client):
    response = client.get("/api/apps", headers={"Sec-Fetch-Site": "sideways"})

    assert response.status_code != 403


# --- item 8a: server_url is validated on save -----------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000",
        "http://x",
        "https://morphe.example.com",
        "https://example.com/morphe",
        "https://example.com/a/b-c_d.e/",
        "http://192.168.1.5:8000",
        "http://[::1]:8000",
    ],
)
def test_a_well_formed_server_url_is_saved(client, url):
    response = client.post(
        "/api/ui/save_settings", json={**SETTINGS, "server_url": url}
    )

    assert response.status_code == 200
    saved = config_store.load_config()["settings"]["server_url"]
    assert saved == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "   ",
        "localhost:8000",
        "ftp://example.com",
        "javascript:alert(1)",
        "http://",
        "http://user@example.com",
        "http://user:pw@example.com",
        "http://example.com?x=1",
        "http://example.com/?x=1",
        "http://example.com#frag",
        "http://exa mple.com",
        "http://example.com/a b",
        "http://example.com/../x",
        "http://example.com/./x",
        "http://example.com//x",
        "http://example.com/%2e%2e/x",
        "http://example.com\\@evil.example",
        "http://example.com\nX: y",
        "http://example.com/\x00",
        "http://example.com:99999",
        "http://example.com:port",
        "http://exämple.com",
        "https://evil.example/" + "a" * 300 + "\x7f",
    ],
)
def test_a_malformed_server_url_is_refused_and_nothing_is_written(
    client, data_dir, url
):
    before = (data_dir / "config.yaml").read_bytes()

    response = client.post(
        "/api/ui/save_settings", json={**SETTINGS, "server_url": url}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == main.SERVER_URL_INVALID
    assert (data_dir / "config.yaml").read_bytes() == before


# --- item 8b: ALLOWED_HOSTS replaces the server URL as the trust anchor -------------------


def test_server_url_host_is_ignored_by_the_allow_list_when_allowed_hosts_is_set():
    names = request_guards.allowed_host_names(
        "https://proxy.example", "https://morphe.example.com"
    )

    assert names == frozenset({"morphe.example.com"})


def test_server_url_host_still_counts_when_allowed_hosts_is_unset():
    for unset in (None, "", "  "):
        names = request_guards.allowed_host_names("https://proxy.example", unset)

        assert names == frozenset({"proxy.example"})


def test_server_url_is_not_an_origin_when_allowed_hosts_is_set():
    assert not request_guards.is_origin_allowed(
        "https://proxy.example",
        "10.0.0.2:8000",
        "https://proxy.example",
        "https://morphe.example.com",
    )
    assert request_guards.is_origin_allowed(
        "https://proxy.example", "10.0.0.2:8000", "https://proxy.example", None
    )


def test_full_origin_entries_keep_working_when_allowed_hosts_is_set():
    assert request_guards.is_origin_allowed(
        "https://morphe.example.com",
        "10.0.0.2:8000",
        "https://proxy.example",
        "https://morphe.example.com",
    )


def test_route_refuses_a_server_url_only_host_when_allowed_hosts_is_set(
    client, monkeypatch
):
    set_server_url("https://proxy.example")
    monkeypatch.setenv("ALLOWED_HOSTS", "https://testserver")

    response = client.get("/api/ui/build_status", headers={"Host": "proxy.example"})

    assert response.status_code == 400
    assert "ALLOWED_HOSTS" in response.json()["detail"]
    assert "does not allow hosts" in response.json()["detail"]


def test_route_still_accepts_the_server_url_host_when_allowed_hosts_is_unset(
    client, no_allowed_hosts
):
    set_server_url("https://proxy.example")

    response = client.get("/api/ui/build_status", headers={"Host": "proxy.example"})

    assert response.status_code == 200


def test_origin_refusal_names_allowed_hosts_when_it_is_set(client, monkeypatch):
    set_server_url("https://proxy.example")
    monkeypatch.setenv("ALLOWED_HOSTS", "https://testserver")

    response = client.delete(
        SECRET, headers={"Origin": "https://proxy.example", "Host": "10.0.0.2:8000"}
    )

    assert response.status_code == 403
    assert "ALLOWED_HOSTS" in response.json()["detail"]
