"""search() must not return a page that is not the requested package or version."""

from unittest.mock import MagicMock

from src import apkmirror_downloader as downloader

RESULTS = (
    '<html><div class="appRow"><a class="fontBlack" href="/apk/x/y/{slug}/">'
    "{title}</a></div></html>"
)
FINAL = (
    '<html><h2 class="tabs-header">{title}</h2>'
    '<div class="appspec-value">Package: {package} Downloads: 5</div>'
    '<div class="appspec-value">Version: {version} (123)</div>'
    '<img id="primaryimage" src="/icon.png">'
    '<a class="downloadButton" href="/apk/x/y/z/download/">Download</a></html>'
)


def _scraper(monkeypatch, row_title: str, package: str, version: str):
    instance = object.__new__(downloader.ApkMirror_Downloader)
    monkeypatch.setattr(downloader, "resolve_variant_url", lambda *a: None)
    instance._goto = MagicMock()
    instance.page = MagicMock()
    instance.page.content.side_effect = [
        RESULTS.format(slug="s", title=row_title),
        FINAL.format(title=row_title, package=package, version=version),
    ]
    return instance


def test_fallback_result_for_other_package_is_rejected(monkeypatch):
    instance = _scraper(monkeypatch, "CapCut 15.0.0", "com.lemon.lvoverseas", "15.0.0")

    assert instance.search("eu.livesport.FlashScore_com_plus") is None


def test_fallback_result_for_requested_package_is_kept(monkeypatch):
    instance = _scraper(monkeypatch, "Flashscore 26.10.2", "eu.app", "26.10.2")

    result = instance.search("eu.app")

    assert result is not None and result.package_name == "eu.app"


def test_row_whose_title_merely_contains_the_version_is_rejected(monkeypatch):
    instance = _scraper(monkeypatch, "App 1.2.34", "eu.app", "1.2.34")

    assert instance.search("eu.app", "1.2.3") is None


def test_exact_version_row_is_kept(monkeypatch):
    instance = _scraper(monkeypatch, "App 1.2.3", "eu.app", "1.2.3")

    assert instance.search("eu.app", "1.2.3") is not None


def test_unreadable_package_is_rejected(monkeypatch):
    instance = _scraper(monkeypatch, "App 1.2.3", "Unknown", "1.2.3")

    assert instance.search("eu.app", "1.2.3") is None
