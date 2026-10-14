"""Tests for resolving an APKMirror variant page through the Morphe API."""

import urllib.error
from email.message import Message
from unittest.mock import MagicMock

import pytest

from src import apkmirror_downloader as downloader

VARIANT = "https://www.apkmirror.com/apk/a/b/b-1-release/b-1-android-apk-download/"
RESOLVED = downloader.ResolvedVariant(downloader.VariantSource.APKMIRROR, VARIANT)
UPTODOWN = "https://flashscore.en.uptodown.com/android/download/1223075994-x"


def _redirect(location: str, code: int = 302) -> urllib.error.HTTPError:
    headers = Message()
    headers["Location"] = location
    return urllib.error.HTTPError("u", code, "redirect", headers, None)


def _opener(monkeypatch, *responses):
    opener = MagicMock()
    opener.open.side_effect = list(responses)
    monkeypatch.setattr(
        downloader.urllib.request, "build_opener", lambda *handlers: opener
    )
    return opener


def test_returns_apkmirror_variant_url(monkeypatch):
    opener = _opener(monkeypatch, _redirect(VARIANT))

    result = downloader.resolve_variant_url("com.a", "1", "arm64-v8a")

    assert result == RESOLVED
    sent = opener.open.call_args.args[0]
    assert sent.full_url.endswith("/v2/web-search/com.a~1~arm64-v8a")


def test_follows_relative_fallback_route(monkeypatch):
    _opener(
        monkeypatch, _redirect("/v2/web-search-fallback/com.a~1~x"), _redirect(VARIANT)
    )

    assert downloader.resolve_variant_url("com.a", "1", "x") == RESOLVED


@pytest.mark.parametrize(
    "location",
    [
        "https://google.com/search?q=x",
        "http://www.apkmirror.com/a/",
        "https://evil.test/",
    ],
)
def test_rejects_foreign_or_insecure_redirects(monkeypatch, location):
    _opener(monkeypatch, _redirect(location))

    assert downloader.resolve_variant_url("com.a", "1", "x") is None


def test_returns_none_when_unreachable(monkeypatch):
    _opener(monkeypatch, urllib.error.URLError("down"))

    assert downloader.resolve_variant_url("com.a", "1", "x") is None


def test_returns_none_on_http_error_without_redirect(monkeypatch):
    _opener(monkeypatch, urllib.error.HTTPError("u", 403, "no", Message(), None))

    assert downloader.resolve_variant_url("com.a", "1", "x") is None


def test_search_uses_resolved_metadata(monkeypatch):
    instance = object.__new__(downloader.ApkMirror_Downloader)
    monkeypatch.setattr(downloader, "resolve_variant_url", lambda *a: RESOLVED)
    metadata = MagicMock()
    instance._goto = MagicMock()
    instance.page = MagicMock()
    instance.page.content.return_value = "<html></html>"
    instance._extract_final_metadata = MagicMock(return_value=metadata)

    assert instance.search("com.a", "1") is metadata
    instance._goto.assert_called_once_with(VARIANT)


def _resolved_instance(monkeypatch, spec_text: str):
    instance = object.__new__(downloader.ApkMirror_Downloader)
    monkeypatch.setattr(downloader, "resolve_variant_url", lambda *a: RESOLVED)
    instance._goto = MagicMock()
    instance.page = MagicMock()
    instance.page.content.return_value = (
        f'<html><div class="appspec-value">{spec_text}</div></html>'
    )
    instance._extract_final_metadata = MagicMock(return_value="metadata")
    return instance


def test_resolved_variant_meeting_minimum_sdk_is_used(monkeypatch):
    instance = _resolved_instance(monkeypatch, "Min: Android 11 (API 30)Target: 16")

    assert instance.search("com.a", "1", minimum_sdk=30) == "metadata"


@pytest.mark.parametrize("spec_text", ["Min: Android 9 (API 28)", "no spec here"])
def test_resolved_variant_below_or_without_minimum_sdk_falls_back(
    monkeypatch, spec_text
):
    instance = _resolved_instance(monkeypatch, spec_text)
    instance._build_search_url = MagicMock(return_value="search-url")
    instance._goto.side_effect = [None, RuntimeError("stop")]

    assert instance.search("com.a", "1", minimum_sdk=30) is None
    assert instance._goto.call_args_list[-1].args == ("search-url",)


def test_returns_uptodown_url_tagged_as_uptodown(monkeypatch):
    _opener(monkeypatch, _redirect(UPTODOWN))

    result = downloader.resolve_variant_url("com.a", "1", "arm64-v8a")

    assert result == downloader.ResolvedVariant(
        downloader.VariantSource.UPTODOWN, UPTODOWN
    )


def test_uptodown_reached_through_fallback_route(monkeypatch):
    _opener(
        monkeypatch,
        _redirect("/v2/web-search-fallback/com.a~1~x"),
        _redirect(UPTODOWN),
    )

    result = downloader.resolve_variant_url("com.a", "1", "x")

    assert result is not None
    assert result.source is downloader.VariantSource.UPTODOWN


@pytest.mark.parametrize(
    "location",
    [
        "http://flashscore.en.uptodown.com/android/download/1-x",
        "https://uptodown.com.evil.test/android/download/1-x",
        "https://evil-uptodown.com/android/download/1-x",
        "https://notuptodown.com/android/download/1-x",
        "https://uptodown.com@evil.test/android/download/1-x",
        "https://flashscore.en.uptodown.com:8443/android/download/1-x",
        "https://uptodown.net/android/download/1-x",
    ],
)
def test_rejects_lookalike_uptodown_hosts(monkeypatch, location):
    _opener(monkeypatch, _redirect(location))

    assert downloader.resolve_variant_url("com.a", "1", "x") is None


def test_accepts_bare_uptodown_domain(monkeypatch):
    _opener(monkeypatch, _redirect("https://uptodown.com/android/download/1-x"))

    result = downloader.resolve_variant_url("com.a", "1", "x")

    assert result is not None
    assert result.source is downloader.VariantSource.UPTODOWN


def test_hop_limit_still_applies_to_uptodown(monkeypatch):
    hop = _redirect("/v2/web-search-fallback/com.a~1~x")
    opener = _opener(monkeypatch, hop, hop, hop, _redirect(UPTODOWN))

    assert downloader.resolve_variant_url("com.a", "1", "x") is None
    assert opener.open.call_count == 3


def test_morphe_search_ignores_uptodown_result(monkeypatch):
    instance = object.__new__(downloader.ApkMirror_Downloader)
    monkeypatch.setattr(
        downloader,
        "resolve_variant_url",
        lambda *a: downloader.ResolvedVariant(
            downloader.VariantSource.UPTODOWN, UPTODOWN
        ),
    )
    instance._goto = MagicMock()

    assert instance._search_via_morphe("com.a", "1", "arm64-v8a") is None
    instance._goto.assert_not_called()
