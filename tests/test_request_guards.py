"""Pure decision logic of the Host allow-list and the Origin check."""

import pytest

from src import request_guards

SERVER_URL = "https://morphe.example.com"


@pytest.mark.parametrize(
    "header",
    [
        "127.0.0.1",
        "127.0.0.1:8000",
        "192.168.1.5:8000",
        "10.0.0.2",
        "[::1]",
        "[::1]:8000",
        "[fe80::1]:80",
        "[2001:db8::7]",
        "localhost",
        "localhost:8000",
        "LocalHost:8000",
        "localhost.",
        "localhost.:8000",
        "nas.lan",
        "NAS.lan.:8000",
    ],
)
def test_allowed_hosts(header):
    names = request_guards.allowed_host_names(SERVER_URL, "nas.lan, other.local")

    assert request_guards.is_host_allowed(header, names)


@pytest.mark.parametrize(
    "header", ["morphe.example.com", "Morphe.Example.COM:443", "morphe.example.com."]
)
def test_server_url_host_is_allowed_while_allowed_hosts_is_unset(header):
    names = request_guards.allowed_host_names(SERVER_URL, None)

    assert request_guards.is_host_allowed(header, names)


@pytest.mark.parametrize("header", ["morphe.example.com", "Morphe.Example.COM:443"])
def test_server_url_host_is_not_allowed_once_allowed_hosts_is_set(header):
    names = request_guards.allowed_host_names(SERVER_URL, "https://nas.lan, other.local")

    assert not request_guards.is_host_allowed(header, names)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        " ",
        "evil.example",
        "evil.example:8000",
        "127.0.0.1.evil.example",
        "localhost.evil.example",
        "::1",
        "[::1",
        "::1]",
        "[::1]x",
        "[1.2.3.4]",
        "[localhost]",
        "user@localhost",
        "localhost@evil.example",
        "user:pw@127.0.0.1",
        "localhost/evil",
        "localhost?x",
        "localhost#x",
        "local host",
        "localhost\n",
        "localhost\r\nX: y",
        "local\x00host",
        "localhost:",
        "localhost:abc",
        "localhost:99999",
        "localhost:80:80",
        "<script>alert(1)</script>",
        "0x7f000001",
        "2130706433",
        "1.2.3",
        "300.1.1.1",
        "exämple.com",
        ".",
        "..",
        "-",
        "a" * 300,
    ],
)
def test_refused_hosts(header):
    names = request_guards.allowed_host_names(SERVER_URL, "nas.lan")

    assert not request_guards.is_host_allowed(header, names)


def test_unknown_name_refused_without_configuration():
    names = request_guards.allowed_host_names(None, None)

    assert not request_guards.is_host_allowed("morphe.example.com", names)
    assert request_guards.is_host_allowed("localhost", names)
    assert request_guards.is_host_allowed("10.1.2.3:8000", names)


@pytest.mark.parametrize("server_url", [None, "", "not a url", "http://", "//", 5])
def test_unusable_server_url_adds_nothing(server_url):
    assert request_guards.allowed_host_names(server_url, None) == frozenset()


def test_allowed_hosts_env_parsing_ignores_junk():
    names = request_guards.allowed_host_names(
        None, " A.lan , ,b.lan:8000,bad host,c@d, e.lan. "
    )

    assert names == {"a.lan", "b.lan", "e.lan"}


@pytest.mark.parametrize(
    ("origin", "host", "allowed"),
    [
        ("http://localhost:8000", "localhost:8000", True),
        ("http://LOCALHOST:8000", "localhost:8000", True),
        ("http://localhost.:8000", "localhost:8000", True),
        ("http://[::1]:8000", "[::1]:8000", True),
        ("http://192.168.1.5:8000", "192.168.1.5:8000", True),
        # Host without a port takes the default port of the Origin scheme.
        ("http://nas.lan", "nas.lan", True),
        ("https://nas.lan", "nas.lan", True),
        ("https://nas.lan:443", "nas.lan", True),
        ("http://nas.lan:8000", "nas.lan", False),
        # Another host or port.
        ("http://evil.example", "localhost:8000", False),
        ("http://localhost:9999", "localhost:8000", False),
        ("http://localhost", "localhost:8000", False),
        ("http://localhost:8000.evil.example", "localhost:8000", False),
        ("http://evil.example:8000", "localhost:8000", False),
        ("http://localhost.evil.example:8000", "localhost:8000", False),
        # Same host and port with another scheme is the same address.
        ("https://localhost:8000", "localhost:8000", True),
        # Not an origin.
        ("null", "localhost:8000", False),
        ("NULL", "localhost:8000", False),
        ("", "localhost:8000", False),
        ("localhost:8000", "localhost:8000", False),
        ("file://localhost:8000", "localhost:8000", False),
        ("ftp://localhost:8000", "localhost:8000", False),
        ("http://user@localhost:8000", "localhost:8000", False),
        ("http://localhost:8000/path", "localhost:8000", False),
        ("http://localhost:abc", "localhost:8000", False),
        ("http://localhost:99999", "localhost:8000", False),
        ("http://", "localhost:8000", False),
        ("http://localhost:8000\n", "localhost:8000", False),
        # No usable Host: nothing to compare with.
        ("http://localhost:8000", None, False),
        ("http://localhost:8000", "bad host", False),
    ],
)
def test_origin_against_host(origin, host, allowed):
    assert request_guards.is_origin_allowed(origin, host, None) is allowed


@pytest.mark.parametrize(
    ("origin", "host", "allowed"),
    [
        ("https://morphe.example.com", "127.0.0.1:8000", True),
        ("https://MORPHE.example.com:443", "127.0.0.1:8000", True),
        ("https://morphe.example.com:8443", "127.0.0.1:8000", False),
        ("http://morphe.example.com", "127.0.0.1:8000", False),
        ("https://evil.example", "127.0.0.1:8000", False),
        ("null", "127.0.0.1:8000", False),
    ],
)
def test_origin_matching_server_url(origin, host, allowed):
    assert request_guards.is_origin_allowed(origin, host, SERVER_URL) is allowed


def test_server_url_with_port_matches_its_port_only():
    server_url = "http://morphe.lan:8000"

    assert request_guards.is_origin_allowed(
        "http://morphe.lan:8000", "10.0.0.2:8000", server_url
    )
    assert not request_guards.is_origin_allowed(
        "http://morphe.lan:9000", "10.0.0.2:8000", server_url
    )
    assert not request_guards.is_origin_allowed(
        "http://morphe.lan", "10.0.0.2:8000", server_url
    )


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "get"])
def test_safe_methods_skip_the_origin_check(method):
    assert not request_guards.is_state_changing(method)


@pytest.mark.parametrize(
    "method", ["POST", "PUT", "DELETE", "PATCH", "post", "PROPFIND", "TRACE"]
)
def test_other_methods_are_state_changing(method):
    assert request_guards.is_state_changing(method)


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        # A full-origin entry trusts that exact scheme, host and port.
        ("https://morphe.example.com", True),
        ("HTTPS://Morphe.Example.com.", True),
        ("https://morphe.example.com:443", True),
        ("http://morphe.example.com", False),
        ("https://morphe.example.com:8443", False),
        ("https://sub.morphe.example.com", False),
        ("https://evil.example", False),
        ("null", False),
        ("https://nas.lan:8443", True),
        ("https://nas.lan", False),
        ("http://nas.lan:8443", False),
        ("http://other.lan:8000", True),
        ("http://other.lan", False),
        ("http://other.lan:9000", False),
        # A bare name allows that Host only, never an Origin.
        ("http://bare.lan", False),
        ("https://bare.lan", False),
        ("http://bare.lan:8000", False),
        ("https://port.lan:8443", False),
    ],
)
def test_only_full_origin_entries_trust_an_origin(origin, allowed):
    env = (
        "https://morphe.example.com/, https://nas.lan:8443, http://other.lan:8000,"
        " bare.lan, port.lan:8443"
    )

    assert (
        request_guards.is_origin_allowed(origin, "10.0.0.2:8000", None, env) is allowed
    )


@pytest.mark.parametrize(
    "entry",
    [
        "ftp://x.example",
        "https://",
        "https://user@x.example",
        "https://x.example/path",
        "https://x.example:99999",
        "https://bad host",
        "https://*",
        "//x.example",
        "x.example/",
        "<script>",
    ],
)
def test_junk_entries_trust_nothing(entry):
    assert not request_guards.is_origin_allowed(
        "https://x.example", "1.1.1.1", None, entry
    )
    assert not request_guards.is_host_allowed(
        "x.example", request_guards.allowed_host_names(None, entry)
    )


def test_full_origin_entry_also_allows_its_host_as_a_host():
    names = request_guards.allowed_host_names(
        None, "https://morphe.example.com:8443/, nas.lan, http://Other.lan."
    )

    assert names == {"morphe.example.com", "nas.lan", "other.lan"}
    assert request_guards.is_host_allowed("morphe.example.com:9999", names)


def test_full_origin_entry_with_default_port_matches_explicit_default():
    assert request_guards.is_origin_allowed(
        "http://nas.lan:80", "10.0.0.2:8000", None, "http://nas.lan"
    )
    assert not request_guards.is_origin_allowed(
        "http://nas.lan:8000", "10.0.0.2:8000", None, "http://nas.lan"
    )


def test_loopback_origin_entry_needs_a_loopback_host():
    entry = "http://localhost:8000"

    assert not request_guards.is_origin_allowed(
        "http://localhost:8000", "192.168.1.5:8000", None, entry
    )
    assert request_guards.is_origin_allowed(
        "http://localhost:8000", "127.0.0.1:8000", None, entry
    )


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("localhost:8000", True),
        ("127.0.0.1:8000", True),
        ("127.5.5.5:8000", True),
        ("[::1]:8000", True),
        ("app.localhost:8000", True),
        ("192.168.1.5:8000", False),
        ("10.0.0.2", False),
        ("morphe.example.com", False),
        ("[fe80::1]:8000", False),
        (None, False),
    ],
)
def test_loopback_server_url_needs_a_loopback_host(host, allowed):
    origin = "http://localhost:8000"

    assert (
        request_guards.is_origin_allowed(origin, host, "http://localhost:8000")
        is allowed
    )


@pytest.mark.parametrize(
    "server_url",
    ["http://127.0.0.1:8000", "http://[::1]:8000", "http://127.9.9.9:8000"],
)
def test_other_loopback_server_urls_are_treated_the_same(server_url):
    origin = server_url
    assert not request_guards.is_origin_allowed(origin, "192.168.1.5:8000", server_url)
    assert request_guards.is_origin_allowed(origin, "localhost:8000", server_url)


def test_non_loopback_server_url_works_for_any_host():
    assert request_guards.is_origin_allowed(
        "https://morphe.example.com", "192.168.1.5:8000", "https://morphe.example.com"
    )


@pytest.mark.parametrize(
    "name",
    ["localhost", "a.localhost", "127.0.0.1", "127.255.255.254", "::1", "0.0.0.0"],
)
def test_loopback_names(name):
    assert request_guards.is_loopback(name)


@pytest.mark.parametrize(
    "name", ["localhost.evil.example", "128.0.0.1", "10.0.0.1", "fe80::1", "nas.lan"]
)
def test_not_loopback_names(name):
    assert not request_guards.is_loopback(name)


@pytest.mark.parametrize(
    "origin", ["http://[::]:8000", "http://[::ffff:127.0.0.1]:8000"]
)
def test_unspecified_and_mapped_loopback_origins_need_a_loopback_host(origin):
    server_url = origin

    assert not request_guards.is_origin_allowed(origin, "192.168.1.5:8000", server_url)
    assert request_guards.is_origin_allowed(origin, "[::1]:8000", server_url)
    assert not request_guards.is_origin_allowed(
        origin, "192.168.1.5:8000", None, origin
    )
