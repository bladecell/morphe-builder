"""Tests for the APKMirror scraper resilience code; no network, no real browser."""

import contextlib
import io
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from selectolax.parser import HTMLParser

from src import apkmirror_downloader, patcher
from src.apkmirror_downloader import ApkMirror_Downloader

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SEARCH_URL = "https://www.apkmirror.com/?s=com.instagram.android"
PACKAGE = "com.instagram.android"
NO_RECOMMENDED_CONFIG: dict[str, list[dict[str, str]]] = {"apps": [], "sources": []}


@dataclass
class FakePage:
    """Stand-in for a Playwright page."""

    html: str = ""
    goto_error: PlaywrightError | None = None
    visited: list[str] = field(default_factory=list)

    def goto(self, url: str, wait_until: str) -> None:
        """Record the navigation, then fail if an error is configured."""
        self.visited.append(url)
        if self.goto_error is not None:
            raise self.goto_error

    def wait_for_selector(self, selector: str, timeout: int) -> None:
        """Pretend the selector is already present."""

    def content(self) -> str:
        """Return the configured page HTML."""
        return self.html


@dataclass
class FakeBrowser:
    """Stand-in for a cloakbrowser browser."""

    page: FakePage = field(default_factory=FakePage)
    close_error: PlaywrightError | None = None
    close_calls: int = 0

    def new_page(self) -> FakePage:
        """Return this browser's only page."""
        return self.page

    def close(self) -> None:
        """Count the call, then fail if an error is configured."""
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


@dataclass
class FakeScraper:
    """Stand-in for ApkMirror_Downloader as used by get_supported_versions."""

    newest: str | None = None
    error: PlaywrightError | None = None
    search_calls: list[str] = field(default_factory=list)

    def latest_version(self, package_name: str) -> str | None:
        """Return the configured version, or fail if an error is configured."""
        if self.error is not None:
            raise self.error
        return self.newest

    def search(self, package_name: str) -> None:
        """Record the call; the fallback must never get here."""
        self.search_calls.append(package_name)


@pytest.fixture
def browsers(monkeypatch: pytest.MonkeyPatch) -> list[FakeBrowser]:
    """Two fake browsers that the patched launch() hands out in order."""
    queue = [FakeBrowser(), FakeBrowser()]
    remaining = iter(queue)
    monkeypatch.setattr(apkmirror_downloader, "launch", lambda: next(remaining))
    return queue


@pytest.fixture
def search_html() -> str:
    """HTML of an APKMirror search results page."""
    return (FIXTURES_DIR / "search_results.html").read_text()


@pytest.fixture
def version_cache(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, dict]]:
    """Serve a stale cached version list and collect every saved cache."""
    saved: list[dict[str, dict]] = []
    monkeypatch.setattr(
        patcher,
        "load_version_cache",
        lambda: {PACKAGE: {"versions": ["1.0.0"], "timestamp": 0}},
    )
    monkeypatch.setattr(patcher, "save_version_cache", saved.append)
    return saved


@pytest.mark.parametrize(
    "message",
    [
        "Page.goto: Page crashed",
        "Target page, context or browser has been closed",
    ],
)
def test_goto_relaunches_and_retries_on_dead_browser(
    browsers: list[FakeBrowser], message: str
) -> None:
    """A crashed or closed browser is replaced and the URL is retried."""
    first, second = browsers
    first.page.goto_error = PlaywrightError(message)
    downloader = ApkMirror_Downloader()

    downloader._goto(SEARCH_URL)

    assert first.close_calls == 1
    assert downloader.browser is second
    assert downloader.page is second.page
    assert second.page.visited == [SEARCH_URL]


def test_goto_reraises_unrelated_error_without_relaunch(
    browsers: list[FakeBrowser],
) -> None:
    """A timeout is re-raised and the browser is left alone."""
    first, second = browsers
    first.page.goto_error = PlaywrightTimeoutError(
        "Page.goto: Timeout 30000ms exceeded."
    )
    downloader = ApkMirror_Downloader()

    with pytest.raises(PlaywrightTimeoutError):
        downloader._goto(SEARCH_URL)

    assert first.close_calls == 0
    assert downloader.browser is first
    assert second.page.visited == []


def test_goto_recovers_when_closing_dead_browser_fails(
    browsers: list[FakeBrowser],
) -> None:
    """A failing close() of the dead browser does not stop recovery."""
    first, second = browsers
    first.page.goto_error = PlaywrightError("Page.goto: Page crashed")
    first.close_error = PlaywrightError("Browser.close: Connection closed")
    downloader = ApkMirror_Downloader()

    downloader._goto(SEARCH_URL)

    assert first.close_calls == 1
    assert downloader.browser is second
    assert second.page.visited == [SEARCH_URL]


def test_goto_propagates_error_from_retried_navigation(
    browsers: list[FakeBrowser],
) -> None:
    """A failure of the single retry reaches the caller."""
    first, second = browsers
    first.page.goto_error = PlaywrightError("Page.goto: Page crashed")
    second.page.goto_error = PlaywrightError("Page.goto: net::ERR_CONNECTION_RESET")
    downloader = ApkMirror_Downloader()

    with pytest.raises(PlaywrightError, match="ERR_CONNECTION_RESET"):
        downloader._goto(SEARCH_URL)

    assert second.page.visited == [SEARCH_URL]


@pytest.mark.parametrize(
    "only_release, expected",
    [(True, "449.0.0.52.84"), (False, "450.0.0.0.12")],
)
def test_latest_version_reads_version_of_first_valid_row(
    browsers: list[FakeBrowser], search_html: str, only_release: bool, expected: str
) -> None:
    """Pre-release rows are skipped only when only_release is set."""
    browsers[0].page.html = search_html
    downloader = ApkMirror_Downloader()

    version = downloader.latest_version(PACKAGE, only_release=only_release)

    assert version == expected


def test_latest_version_returns_none_when_no_row_matches(
    browsers: list[FakeBrowser],
) -> None:
    """A page holding only pre-release rows yields no version."""
    beta_only_html = (
        '<div class="appRow"><a class="fontBlack" href="/apk/x/">App 2.0 beta</a></div>'
    )
    browsers[0].page.html = beta_only_html
    downloader = ApkMirror_Downloader()

    assert downloader.latest_version(PACKAGE) is None


def test_latest_version_returns_none_for_row_without_info_slide(
    browsers: list[FakeBrowser], search_html: str
) -> None:
    """A sidebar row has no details block to read a version from."""
    sidebar = HTMLParser(search_html).css_first("#secondary")
    assert sidebar is not None
    browsers[0].page.html = sidebar.html or ""
    downloader = ApkMirror_Downloader()

    assert downloader.latest_version(PACKAGE) is None


def test_extract_first_valid_row_matches_target_version(
    browsers: list[FakeBrowser], search_html: str
) -> None:
    """The row whose title contains the target version is returned."""
    downloader = ApkMirror_Downloader()

    row = downloader._extract_first_valid_row(
        HTMLParser(search_html), "448.0.0.48.109", True
    )

    assert row is not None
    assert row.text(strip=True) == "Instagram 448.0.0.48.109"


def test_extract_first_valid_row_skips_rows_without_title_link(
    browsers: list[FakeBrowser],
) -> None:
    """Rows without a title link are ignored."""
    parser = HTMLParser(
        '<div class="appRow"><span>Advertisement</span></div>'
        '<div class="appRow"><a class="fontBlack" href="/apk/x/">App 1.0</a></div>'
    )
    downloader = ApkMirror_Downloader()

    row = downloader._extract_first_valid_row(parser, None, True)

    assert row is not None
    assert row.text(strip=True) == "App 1.0"


def test_scraper_log_goes_to_stdout_current_at_emit_time() -> None:
    """Scraper logs follow sys.stdout swaps, as the build console needs."""
    console = io.StringIO()

    with contextlib.redirect_stdout(console):
        apkmirror_downloader.logger.warning("routed to build console")

    assert "[WARNING] routed to build console" in console.getvalue()


def test_supported_versions_falls_back_to_newest_from_scraper(
    version_cache: list[dict[str, dict]],
) -> None:
    """The scraper's newest version is returned and cached."""
    scraper = FakeScraper(newest="1.2.3")

    result = patcher.get_supported_versions(
        PACKAGE, NO_RECOMMENDED_CONFIG, scraper=scraper, quiet=True
    )

    assert result == (["1.2.3"], None, None)
    assert [cache[PACKAGE]["versions"] for cache in version_cache] == [["1.2.3"]]
    assert scraper.search_calls == []


@pytest.mark.parametrize(
    "scraper",
    [
        FakeScraper(newest=None),
        FakeScraper(error=PlaywrightTimeoutError("Timeout 15000ms exceeded.")),
    ],
    ids=["no-version-found", "scraper-raises"],
)
def test_supported_versions_returns_cached_list_when_scraper_has_nothing(
    version_cache: list[dict[str, dict]], scraper: FakeScraper
) -> None:
    """A scraper without a result leaves the cached list in place."""
    result = patcher.get_supported_versions(
        PACKAGE, NO_RECOMMENDED_CONFIG, scraper=scraper, quiet=True
    )

    assert result == (["1.0.0"], None, None)
    assert version_cache == []
    assert scraper.search_calls == []
