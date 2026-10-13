"""The GitHub token goes to api.github.com as a header and nowhere else.

Every test drives the code through a recording fake transport that replaces
`HTTPAdapter.send`, so real `requests` logic (header merging, netrc, proxy and
redirect handling) runs and nothing reaches the network.
"""

import json
import logging
import os
import time
import traceback
from dataclasses import dataclass, field

import pytest
import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient
from requests.adapters import HTTPAdapter

from src import main, patcher, secrets_store

TOKEN = "ghp_faketoken0123456789abcdef"
API_URL = "https://api.github.com/repos/own/src/releases/latest"
ASSET_URL = "https://github.com/own/src/releases/download/v1.2.3/patches.mpp"
BODY_MARKER = "response-body-marker-7781"


@dataclass
class Call:
    """One request as it reached the transport."""

    url: str
    headers: dict[str, str]
    proxies: dict[str, str] | None
    verify: object


@dataclass
class Transport:
    """Records requests and answers from routes (longest matching prefix)."""

    calls: list[Call] = field(default_factory=list)
    routes: dict[str, object] = field(default_factory=dict)

    def route(
        self,
        url_prefix: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes | dict | None = None,
        raises: Exception | None = None,
    ) -> None:
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        self.routes[url_prefix] = (status, headers or {}, body or b"", raises)

    def send(self, request, **kwargs):
        self.calls.append(
            Call(
                request.url,
                dict(request.headers),
                kwargs.get("proxies"),
                kwargs.get("verify"),
            )
        )
        matches = [p for p in self.routes if request.url.startswith(p)]
        if not matches:
            return self._response(request, 404, {}, b"")
        status, headers, body, raises = self.routes[max(matches, key=len)]
        if raises is not None:
            raise raises
        return self._response(request, status, headers, body)

    @staticmethod
    def _response(request, status, headers, body):
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response._content = body
        response._content_consumed = True
        response.url = request.url
        response.request = request
        return response

    def hosts(self) -> list[str]:
        return [c.url.split("/")[2] for c in self.calls]

    def with_header(self, name: str) -> list[Call]:
        return [c for c in self.calls if name in {k.title() for k in c.headers}]


@pytest.fixture
def transport(monkeypatch):
    fake = Transport()
    monkeypatch.setattr(
        HTTPAdapter,
        "send",
        lambda self, request, **kwargs: fake.send(request, **kwargs),
    )
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NETRC", os.devnull)
    return fake


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv(secrets_store.GITHUB_TOKEN, TOKEN)
    return TOKEN


def release(tag: str = "v1.2.3", url: str = ASSET_URL, name: str = "patches.mpp"):
    return {
        "tag_name": tag,
        "assets": [{"name": name, "browser_download_url": url, "size": 3}],
    }


def sync_mpp(tmp_path) -> dict:
    return {".mpp": tmp_path / "p.mpp"}


# --- outcome 1: header present when set, absent when unset -------------------


def test_token_is_sent_as_bearer_header_on_the_release_lookup(
    transport, token, tmp_path
):
    transport.route(API_URL, body=release())
    transport.route(ASSET_URL, body=b"mpp")

    patcher.check_and_download_release("own/src", sync_mpp(tmp_path), force=True)

    api_call = transport.calls[0]
    assert api_call.url == API_URL
    assert api_call.headers["Authorization"] == f"Bearer {TOKEN}"


def test_no_header_when_no_token_is_set(transport, tmp_path):
    transport.route(API_URL, body=release())
    transport.route(ASSET_URL, body=b"mpp")

    patcher.check_and_download_release("own/src", sync_mpp(tmp_path), force=True)

    assert transport.calls[0].url == API_URL
    assert transport.with_header("Authorization") == []


def test_token_never_reaches_asset_or_raw_requests(transport, token, tmp_path):
    transport.route(API_URL, body=release())
    transport.route(ASSET_URL, status=302, headers={"Location": "https://cdn.x/a"})
    transport.route("https://cdn.x/", body=b"mpp")
    # A real list at the tag: the bundle is only downloaded once a list was found.
    transport.route("https://raw.githubusercontent.com/", body=b'[{"name": "x"}]')
    paths = {".mpp": tmp_path / "p.mpp", ".json": tmp_path / "p.json"}

    patcher.check_and_download_release("own/src", paths, force=True)

    assert "raw.githubusercontent.com" in transport.hosts()
    assert "cdn.x" in transport.hosts()
    others = [c for c in transport.calls if c.url != API_URL]
    assert others
    for call in others:
        assert "Authorization" not in call.headers
        assert TOKEN not in call.url
        assert TOKEN not in repr(call.headers)


def test_token_is_never_in_a_url_or_body(transport, token, tmp_path):
    transport.route(API_URL, body=release())
    transport.route(ASSET_URL, body=b"mpp")

    patcher.check_and_download_release("own/src", sync_mpp(tmp_path), force=True)

    assert all(TOKEN not in c.url for c in transport.calls)
    assert "?" not in transport.calls[0].url


# --- outcome 2: host refusal, redirects -------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/repos/a/b",
        "http://api.github.com/repos/a/b",
        "https://api.github.com.evil.example/repos/a/b",
        "https://evil.example/https://api.github.com/",
        "https://api.github.com@evil.example/repos/a/b",
        "https://user:pw@api.github.com/repos/a/b",
        "https://api.github.com:8443/repos/a/b",
        "https://raw.githubusercontent.com/own/src/main/patches.json",
        "https://github.com/own/src/releases/download/v1/x.mpp",
        "ftp://api.github.com/x",
        "api.github.com/repos/a/b",
        "",
    ],
)
@pytest.mark.parametrize("with_token", [True, False])
def test_helper_refuses_any_other_target(transport, monkeypatch, url, with_token):
    if with_token:
        monkeypatch.setenv(secrets_store.GITHUB_TOKEN, TOKEN)

    with pytest.raises(patcher.GitHubHostError):
        patcher.github_api_get(url)

    assert transport.calls == []


def test_redirect_to_another_host_is_not_followed_and_carries_nothing(
    transport, token, tmp_path
):
    transport.route(API_URL, status=302, headers={"Location": "https://evil.example/x"})
    transport.route("https://evil.example/", body=release())

    result = patcher.check_and_download_release(
        "own/src", sync_mpp(tmp_path), force=True
    )

    assert result is False
    assert transport.hosts() == ["api.github.com"]
    assert [c.url for c in transport.with_header("Authorization")] == [API_URL]


@pytest.mark.parametrize(
    "location",
    [
        "http://api.github.com/x",
        "https://api.github.com.evil.example/x",
        "https://api.github.com:444/x",
    ],
)
def test_redirect_that_leaves_the_api_origin_is_refused(
    transport, token, tmp_path, location
):
    transport.route(API_URL, status=301, headers={"Location": location})

    patcher.check_and_download_release("own/src", sync_mpp(tmp_path), force=True)

    assert [c.url for c in transport.calls] == [API_URL]


def test_redirect_within_the_api_host_keeps_working_and_keeps_the_header(
    transport, token, tmp_path
):
    moved = "https://api.github.com/repositories/42/releases/latest"
    transport.route(API_URL, status=301, headers={"Location": moved})
    transport.route(moved, body=release())
    transport.route(ASSET_URL, body=b"mpp")

    result = patcher.check_and_download_release(
        "own/src", sync_mpp(tmp_path), force=True
    )

    assert result is True
    api_calls = [c for c in transport.calls if c.url in (API_URL, moved)]
    assert [c.url for c in api_calls] == [API_URL, moved]
    assert all(c.headers["Authorization"] == f"Bearer {TOKEN}" for c in api_calls)


def test_endless_same_host_redirects_are_bounded(transport, token, tmp_path):
    transport.route(API_URL, status=302, headers={"Location": API_URL})

    result = patcher.check_and_download_release(
        "own/src", sync_mpp(tmp_path), force=True
    )

    assert result is False
    assert len(transport.calls) <= 6


# --- outcome 3: netrc and proxy environment ---------------------------------


@pytest.fixture
def netrc_entry(tmp_path, monkeypatch):
    netrc = tmp_path / "netrc"
    netrc.write_text("machine api.github.com login someone password netrc-secret\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))


def test_netrc_entry_cannot_replace_the_token_header(
    transport, token, netrc_entry, tmp_path
):
    transport.route(API_URL, body=release())
    transport.route(ASSET_URL, body=b"mpp")

    patcher.check_and_download_release("own/src", sync_mpp(tmp_path), force=True)

    assert transport.calls[0].headers["Authorization"] == f"Bearer {TOKEN}"


def test_proxy_environment_is_used_without_touching_the_header(
    transport, token, tmp_path, monkeypatch
):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    transport.route(API_URL, body=release())
    transport.route(ASSET_URL, body=b"mpp")

    patcher.check_and_download_release("own/src", sync_mpp(tmp_path), force=True)

    api_call = transport.calls[0]
    assert api_call.proxies is not None
    assert api_call.proxies.get("https") == "http://proxy.local:3128"
    assert api_call.headers["Authorization"] == f"Bearer {TOKEN}"


def test_no_proxy_environment_is_honoured(transport, token, tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    monkeypatch.setenv("NO_PROXY", "api.github.com")
    transport.route(API_URL, body=release())
    transport.route(ASSET_URL, body=b"mpp")

    patcher.check_and_download_release("own/src", sync_mpp(tmp_path), force=True)

    assert not transport.calls[0].proxies


def test_custom_ca_bundle_environment_still_applies(
    transport, token, tmp_path, monkeypatch
):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/etc/corp-ca.pem")
    transport.route(API_URL, body=release())
    transport.route(ASSET_URL, body=b"mpp")

    patcher.check_and_download_release("own/src", sync_mpp(tmp_path), force=True)

    assert transport.calls[0].verify == "/etc/corp-ca.pem"


# --- outcome 4: a rejected token -------------------------------------------


def test_401_raises_fixed_text_naming_the_setting(transport, token, tmp_path):
    transport.route(
        API_URL, status=401, body=f'{{"message": "{BODY_MARKER}"}}'.encode()
    )

    with pytest.raises(patcher.GitHubTokenRejectedError) as raised:
        patcher.check_and_download_release("own/src", sync_mpp(tmp_path), force=True)

    text = f"{raised.value} {raised.value!r} {raised.value.args}"
    assert "GitHub token" in text and "Settings" in text and "401" in text
    assert TOKEN not in text
    assert BODY_MARKER not in text


def test_401_is_not_retried_without_the_token(transport, token, tmp_path):
    transport.route(API_URL, status=401)

    with pytest.raises(patcher.GitHubTokenRejectedError):
        patcher.check_and_download_release("own/src", sync_mpp(tmp_path), force=True)

    assert len(transport.calls) == 1


def test_401_without_a_token_stays_an_ordinary_failure(transport, tmp_path):
    transport.route(API_URL, status=401)

    result = patcher.check_and_download_release(
        "own/src", sync_mpp(tmp_path), force=True
    )

    assert result is False


def test_sync_stops_at_the_first_rejection(transport, token, data_dir):
    transport.route("https://api.github.com/", status=401)
    config = {
        "repositories": {"cli": "x/cli", "apkeditor": "x/edit"},
        "tools": {"cli_jar": "bin/cli.jar", "apkeditor_jar": "bin/edit.jar"},
        "sources": [
            {"name": "A", "repo": "own/a", "active": True},
            {"name": "B", "repo": "own/b", "active": True},
        ],
    }

    with pytest.raises(patcher.GitHubTokenRejectedError):
        patcher.update_tools(config, quiet=True, force=True)

    assert len(transport.calls) == 1


def test_sync_error_on_the_discovery_tab_is_the_fixed_text(monkeypatch):
    def rejected(config, quiet=False, force=False):
        raise patcher.GitHubTokenRejectedError()

    monkeypatch.setattr(main, "update_tools", rejected)

    main.update_tools_task({}, True)

    status = main.get_tools_status()
    assert "GitHub token" in status["sync_error"]
    assert "Settings" in status["sync_error"]
    assert status["updating"] is False


def test_add_source_answers_with_an_error_the_ui_shows(monkeypatch, tmp_path):
    store: dict = {"sources": []}
    paths = {".mpp": tmp_path / "p.mpp", ".json": tmp_path / "p.json"}
    monkeypatch.setattr(main, "load_config", lambda: store)
    monkeypatch.setattr(main, "get_source_paths", lambda repo: paths)

    def rejected(*args, **kwargs):
        raise patcher.GitHubTokenRejectedError()

    monkeypatch.setattr(main, "check_and_download_release", rejected)

    with pytest.raises(HTTPException) as raised:
        main.add_source(main.AddSourcePayload(repo="new/repo"))

    assert raised.value.status_code == 502
    assert "GitHub token" in raised.value.detail
    assert "Settings" in raised.value.detail
    assert store["sources"] == []


# --- the token cannot reach an observer through a failure -------------------


class Observers:
    """Everything a failure can show: exception text, traceback, logs, stdio."""

    def __init__(self, caplog, capsys):
        self.caplog = caplog
        self.capsys = capsys

    def text(self, error: BaseException | None) -> str:
        shown = ""
        if error is not None:
            shown = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
            shown += repr(error) + str(error) + repr(error.args)
            shown += repr(error.__cause__) + repr(error.__context__)
        out = self.capsys.readouterr()
        return shown + self.caplog.text + out.out + out.err


@pytest.fixture
def observers(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    return Observers(caplog, capsys)


LEAKY_FAILURES = [
    requests.exceptions.InvalidHeader(
        f"Invalid leading whitespace in header value: Bearer {TOKEN}"
    ),
    requests.exceptions.ConnectionError(f"boom {{'Authorization': 'Bearer {TOKEN}'}}"),
    ValueError(f"headers={{'Authorization': 'Bearer {TOKEN}'}}"),
    requests.exceptions.SSLError(f"<PreparedRequest [GET]> Bearer {TOKEN}"),
]


@pytest.mark.parametrize("failure", LEAKY_FAILURES, ids=lambda f: type(f).__name__)
def test_helper_failure_carries_no_token(transport, token, observers, failure):
    transport.route(API_URL, raises=failure)
    caught = None

    try:
        patcher.github_api_get(API_URL)
    except Exception as error:  # noqa: BLE001 - the test inspects any escape
        caught = error

    assert caught is not None
    assert TOKEN not in observers.text(caught)
    assert caught.__cause__ is None
    assert caught.__context__ is None


@pytest.mark.parametrize("failure", LEAKY_FAILURES, ids=lambda f: type(f).__name__)
def test_sync_failure_carries_no_token_to_stdout_or_logs(
    transport, token, observers, tmp_path, failure
):
    transport.route(API_URL, raises=failure)

    result = patcher.check_and_download_release(
        "own/src", sync_mpp(tmp_path), force=True
    )

    assert result is False
    assert TOKEN not in observers.text(None)


# --- outcome 5: rate limit reporting -----------------------------------------

RESET_EPOCH = 1_900_000_000
RESET_TEXT = "2030-03-17 17:46 UTC"
LIMITED = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(RESET_EPOCH)}


def test_rate_limit_without_a_token_hints_at_settings(transport, tmp_path):
    transport.route(API_URL, status=403, headers=LIMITED)

    with pytest.raises(patcher.GitHubRateLimitError) as raised:
        patcher.check_and_download_release("own/src", sync_mpp(tmp_path), force=True)

    assert RESET_TEXT in str(raised.value)
    assert "token" in str(raised.value)
    assert "Settings" in str(raised.value)


def test_rate_limit_with_a_token_reports_reset_without_the_hint(
    transport, token, tmp_path
):
    transport.route(API_URL, status=403, headers=LIMITED)

    with pytest.raises(patcher.GitHubRateLimitError) as raised:
        patcher.check_and_download_release("own/src", sync_mpp(tmp_path), force=True)

    assert RESET_TEXT in str(raised.value)
    assert "Settings" not in str(raised.value)
    assert TOKEN not in str(raised.value)


# --- outcome 6: a token that carries permissions ------------------------------


def lookup_with_scopes(transport, scopes: str | None, tmp_path) -> None:
    headers = {} if scopes is None else {"X-OAuth-Scopes": scopes}
    transport.route(API_URL, headers=headers, body=release())
    transport.route(ASSET_URL, body=b"mpp")
    patcher.check_and_download_release("own/src", sync_mpp(tmp_path), force=True)


@pytest.fixture(autouse=True)
def fresh_scope_state(monkeypatch):
    monkeypatch.setattr(patcher, "_token_state", None, raising=False)


def test_classic_token_with_scopes_is_reported_by_scope_name(
    transport, token, tmp_path
):
    lookup_with_scopes(transport, "repo, gist", tmp_path)

    warning = patcher.github_token_warning()

    assert warning is not None
    assert "repo" in warning and "gist" in warning
    assert TOKEN not in warning


def test_classic_token_without_scopes_gives_no_warning(transport, token, tmp_path):
    lookup_with_scopes(transport, "", tmp_path)

    assert patcher.github_token_warning() is None


def test_fine_grained_token_has_no_scope_header_and_gives_no_warning(
    transport, token, tmp_path
):
    lookup_with_scopes(transport, None, tmp_path)

    assert patcher.github_token_warning() is None


def test_odd_scope_text_is_not_echoed(transport, token, tmp_path):
    lookup_with_scopes(transport, f"repo, <b>x</b>, {'a' * 500}, {TOKEN}", tmp_path)

    warning = patcher.github_token_warning()

    assert warning is not None and "repo" in warning
    assert "<b>" not in warning and "a" * 100 not in warning
    assert TOKEN not in warning
    assert len(warning) < 300


def test_warning_goes_away_when_the_token_is_removed(
    transport, token, tmp_path, monkeypatch
):
    lookup_with_scopes(transport, "repo", tmp_path)
    monkeypatch.delenv(secrets_store.GITHUB_TOKEN)

    assert patcher.github_token_warning() is None


def test_warning_is_in_the_status_the_ui_polls(transport, token, tmp_path, data_dir):
    lookup_with_scopes(transport, "repo", tmp_path)

    body = TestClient(main.app).get("/api/ui/tools_status").json()

    assert "repo" in body["github_token_warning"]
    assert TOKEN not in json.dumps(body)


def test_status_has_no_warning_without_a_lookup(token, data_dir):
    body = TestClient(main.app).get("/api/ui/tools_status").json()

    assert body["github_token_warning"] is None


# --- outcome 7: URLs from GitHub's responses ----------------------------------

HOSTILE_TAGS = [
    "v1%2F..%2F..%2Fevil",
    "v1%2e%2e%2fx",
    "v1#frag",
    "v1?x=1",
    "../x",
    "a/../b",
    "a//b",
    "a/./b",
    "..",
    ".",
    "v1 2",
    "v1\n",
    "v1\\x",
    "-v",
    "",
    None,
    123,
    "v" * 200,
]


@pytest.mark.parametrize("tag", HOSTILE_TAGS, ids=repr)
def test_hostile_tag_never_builds_a_raw_url(transport, token, tmp_path, tag):
    transport.route(API_URL, body={"tag_name": tag, "assets": []})
    transport.route("https://raw.githubusercontent.com/", body=b"{}")

    patcher.check_and_download_release(
        "own/src", {".json": tmp_path / "p.json"}, force=True
    )

    assert "raw.githubusercontent.com" not in transport.hosts()


@pytest.mark.parametrize(
    "tag", ["v1.2.3", "1.0.0-beta+1", "patches_v2", "a/b", "release/1.0"]
)
def test_ordinary_tag_still_fetches_from_raw(transport, token, tmp_path, tag):
    transport.route(API_URL, body={"tag_name": tag, "assets": []})
    transport.route("https://raw.githubusercontent.com/", body=b"{}")

    patcher.check_and_download_release(
        "own/src", {".json": tmp_path / "p.json"}, force=True
    )

    raw = [c.url for c in transport.calls if "raw.githubusercontent.com" in c.url]
    assert (
        raw[0] == f"https://raw.githubusercontent.com/own/src/{tag}/patches-list.json"
    )


HOSTILE_ASSET_URLS = [
    "http://github.com/own/src/releases/download/v1/x.mpp",
    "https://evil.example/own/src/releases/download/v1/x.mpp",
    "https://github.com.evil.example/own/src/releases/download/v1/x.mpp",
    "https://github.com@evil.example/own/src/releases/download/v1/x.mpp",
    "https://user@github.com/own/src/releases/download/v1/x.mpp",
    "https://github.com:8443/own/src/releases/download/v1/x.mpp",
    "https://api.github.com/repos/own/src/releases/assets/1",
    "https://raw.githubusercontent.com/own/src/main/x.mpp",
    "https://github.com/own/src/archive/x.mpp",
    "https://github.com/own/src/releases/download/../../../evil/x.mpp",
    "https://github.com/own/src/releases/download/%2e%2e/x.mpp",
    "https://github.com/own/src/releases/download/v1/x.mpp?token=1",
    "https://github.com/own/src/releases/download/v1/x.mpp#f",
    "https://github.com\\@evil.example/own/src/releases/download/v1/x.mpp",
    "file:///etc/passwd",
    "//evil.example/x.mpp",
    "",
    None,
]


@pytest.mark.parametrize("url", HOSTILE_ASSET_URLS, ids=repr)
def test_hostile_asset_url_is_refused_before_any_request(
    transport, token, tmp_path, url
):
    transport.route(API_URL, body=release(url=url))

    result = patcher.check_and_download_release(
        "own/src", sync_mpp(tmp_path), force=True
    )

    assert result is False
    assert transport.hosts() == ["api.github.com"]
    assert not (tmp_path / "p.mpp").exists()


@pytest.mark.parametrize("url", HOSTILE_ASSET_URLS[:6], ids=repr)
def test_hostile_json_asset_url_is_refused_before_any_request(
    transport, token, tmp_path, url
):
    body = release(url=url, name="patches-list.json")
    transport.route(API_URL, body=body)

    patcher.check_and_download_release(
        "own/src", {".json": tmp_path / "p.json"}, force=True
    )

    assert [h for h in transport.hosts() if h != "raw.githubusercontent.com"] == [
        "api.github.com"
    ]


def test_real_asset_url_is_accepted(transport, tmp_path):
    transport.route(API_URL, body=release())
    transport.route(ASSET_URL, status=302, headers={"Location": CDN_URL})
    transport.route(CDN_URL, body=b"mpp")

    result = patcher.check_and_download_release(
        "own/src", sync_mpp(tmp_path), force=True
    )

    assert result is True
    assert (tmp_path / "p.mpp").read_bytes() == b"mpp"


CDN_URL = "https://release-assets.githubusercontent.com/github-production-release-asset/1/2?sig=3"


# --- the secret store stays untouched ---------------------------------------


def test_token_is_never_written_to_the_environment(transport, token, tmp_path):
    transport.route(API_URL, body=release())
    transport.route(ASSET_URL, body=b"mpp")
    before = dict(os.environ)

    patcher.check_and_download_release("own/src", sync_mpp(tmp_path), force=True)

    assert dict(os.environ) == before


# --- the scope warning stands on its own terms, and follows the token in use ----

SECOND_TOKEN = "ghp_secondtoken0123456789abcdef"


def status_body() -> dict:
    return TestClient(main.app).get("/api/ui/tools_status").json()


def test_a_remembered_word_does_not_remove_a_scope_from_the_warning(
    transport, monkeypatch, tmp_path, data_dir
):
    monkeypatch.setenv(secrets_store.GITHUB_TOKEN, TOKEN)
    monkeypatch.setenv(secrets_store.CLOAKBROWSER_LICENSE_KEY, "workflow")
    secrets_store.redact("")  # the licence key is now remembered for masking
    lookup_with_scopes(transport, "repo, workflow", tmp_path)

    warning = patcher.github_token_warning()

    assert warning is not None and "workflow" in warning
    assert "workflow" in status_body()["github_token_warning"]


def test_scope_header_is_bounded_and_limited_to_plain_names(
    transport, token, tmp_path
):
    many = ", ".join(f"scope{n}" for n in range(60))
    lookup_with_scopes(transport, f"repo, UPPER, a b, {'z' * 40}, {many}", tmp_path)

    warning = patcher.github_token_warning()

    assert warning is not None and "repo" in warning
    assert "UPPER" not in warning and "a b" not in warning and "z" * 40 not in warning
    assert warning.count("scope") <= 20
    assert len(warning) < 400


def test_an_oversized_scope_header_is_ignored(transport, token, tmp_path):
    lookup_with_scopes(transport, "repo," + "x," * 5000, tmp_path)

    assert patcher.github_token_warning() is None


def file_token(value: str) -> None:
    secrets_store.set_secret(secrets_store.GITHUB_TOKEN, value)


def test_warning_goes_away_when_the_token_is_replaced(transport, data_dir, tmp_path):
    file_token(TOKEN)
    lookup_with_scopes(transport, "repo", tmp_path)
    assert patcher.github_token_warning() is not None

    file_token(SECOND_TOKEN)

    assert patcher.github_token_warning() is None
    assert status_body()["github_token_warning"] is None


def test_warning_goes_away_when_the_token_is_cleared(transport, data_dir, tmp_path):
    file_token(TOKEN)
    lookup_with_scopes(transport, "repo", tmp_path)

    secrets_store.clear_secret(secrets_store.GITHUB_TOKEN)

    assert patcher.github_token_warning() is None


def test_warning_comes_back_for_the_new_token_after_its_own_lookup(
    transport, data_dir, tmp_path
):
    file_token(TOKEN)
    lookup_with_scopes(transport, "repo", tmp_path)
    file_token(SECOND_TOKEN)
    lookup_with_scopes(transport, "gist", tmp_path)

    warning = patcher.github_token_warning()

    assert warning is not None and "gist" in warning and "repo" not in warning


def test_the_token_is_not_kept_next_to_the_scopes(transport, token, tmp_path):
    lookup_with_scopes(transport, "repo", tmp_path)

    assert TOKEN not in repr(patcher._token_state)


# --- a rejected token is a lasting warning --------------------------------------


def reject(transport, tmp_path) -> None:
    transport.route(API_URL, status=401)
    with pytest.raises(patcher.GitHubTokenRejectedError):
        patcher.check_and_download_release("own/src", sync_mpp(tmp_path), force=True)


def test_a_rejected_token_stays_reported_in_the_status(transport, token, tmp_path):
    reject(transport, tmp_path)

    body = status_body()

    assert body["github_token_rejected"] == patcher.TOKEN_REJECTED_MESSAGE
    assert TOKEN not in json.dumps(body)
    assert status_body()["github_token_rejected"] is not None  # and it stays


def test_a_rejected_token_is_still_not_retried_without_the_token(
    transport, token, tmp_path
):
    reject(transport, tmp_path)

    assert transport.with_header("Authorization")
    assert len(transport.calls) == 1


def test_replacing_the_rejected_token_clears_the_warning(transport, data_dir, tmp_path):
    file_token(TOKEN)
    reject(transport, tmp_path)

    file_token(SECOND_TOKEN)

    assert status_body()["github_token_rejected"] is None


def test_clearing_the_rejected_token_clears_the_warning(transport, data_dir, tmp_path):
    file_token(TOKEN)
    reject(transport, tmp_path)

    secrets_store.clear_secret(secrets_store.GITHUB_TOKEN)

    assert status_body()["github_token_rejected"] is None


def test_a_later_authenticated_success_clears_the_warning(transport, token, tmp_path):
    reject(transport, tmp_path)

    lookup_with_scopes(transport, "", tmp_path)

    assert status_body()["github_token_rejected"] is None


def test_no_rejection_is_reported_for_a_token_never_rejected(token, data_dir):
    assert status_body()["github_token_rejected"] is None


# --- downloads ignore the operator's netrc ----------------------------------------


def with_netrc(monkeypatch, tmp_path) -> None:
    netrc = tmp_path / "netrc"
    netrc.write_text("machine github.com login netrcuser password netrcpass\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))


def test_asset_download_does_not_send_netrc_credentials(
    transport, monkeypatch, tmp_path
):
    with_netrc(monkeypatch, tmp_path)
    transport.route(ASSET_URL, body=b"mpp")

    patcher.download_with_progress(ASSET_URL, tmp_path / "p.mpp")

    assert (tmp_path / "p.mpp").read_bytes() == b"mpp"
    assert transport.with_header("Authorization") == []


def test_bounded_download_does_not_send_netrc_credentials(
    transport, monkeypatch, tmp_path
):
    with_netrc(monkeypatch, tmp_path)
    transport.route(ASSET_URL, body=b"[]")

    patcher._download_bounded(ASSET_URL, tmp_path / "p.json", 1024)

    assert transport.with_header("Authorization") == []


# --- github_token_permissions: the state the Settings page shows ---------------


def test_permissions_is_null_without_a_token(data_dir):
    assert patcher.github_token_permissions() is None


def test_permissions_is_unchecked_before_any_request(token, data_dir):
    assert patcher.github_token_permissions() == "unchecked"


def test_permissions_is_none_for_a_classic_token_without_scopes(
    transport, token, tmp_path
):
    lookup_with_scopes(transport, "", tmp_path)

    assert patcher.github_token_permissions() == "none"


def test_permissions_is_present_for_a_classic_token_with_scopes(
    transport, token, tmp_path
):
    lookup_with_scopes(transport, "repo", tmp_path)

    assert patcher.github_token_permissions() == "present"


def test_permissions_is_unverifiable_without_a_scope_header(
    transport, token, tmp_path
):
    lookup_with_scopes(transport, None, tmp_path)

    assert patcher.github_token_permissions() == "unverifiable"


def test_permissions_is_unchecked_after_the_token_was_rejected(
    transport, token, tmp_path
):
    transport.route(API_URL, status=401)

    with pytest.raises(patcher.GitHubTokenRejectedError):
        patcher.check_and_download_release("own/src", sync_mpp(tmp_path), force=True)

    assert patcher.github_token_permissions() == "unchecked"


def test_permissions_is_unchecked_after_the_token_was_replaced(
    transport, token, tmp_path, monkeypatch
):
    lookup_with_scopes(transport, "repo", tmp_path)
    monkeypatch.setenv(secrets_store.GITHUB_TOKEN, "ghp_anotherfaketoken987654321")

    assert patcher.github_token_permissions() == "unchecked"


def test_status_reports_permissions_and_server_time(
    transport, token, tmp_path, data_dir
):
    lookup_with_scopes(transport, None, tmp_path)

    body = TestClient(main.app).get("/api/ui/tools_status").json()

    assert body["github_token_permissions"] == "unverifiable"
    assert body["github_token_warning"] is None
    assert abs(body["server_time"] - time.time()) < 5
    assert TOKEN not in json.dumps(body)
