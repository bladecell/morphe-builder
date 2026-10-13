"""Explicit cloakbrowser key handling and worker task-boundary rotation."""

import os
from pathlib import Path

import pytest
from cloakbrowser import CloakBrowserLicenseError  # type: ignore[import-untyped]
from cloakbrowser.license import resolve_license_key  # type: ignore[import-untyped]
from selectolax.parser import HTMLParser

from src import main, patcher
from src.apkmirror_downloader import ApkMirror_Downloader, LicenseFallbackError


class FakeBrowser:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1

    def new_page(self) -> object:
        return object()


class DeniedNavigationPage:
    def goto(self, *_args: object, **_kwargs: object) -> None:
        raise CloakBrowserLicenseError(
            "CloakBrowser Pro: license key is invalid, expired, or missing. "
            "Check CLOAKBROWSER_LICENSE_KEY."
        )


class DeniedSelectorPage:
    def __init__(self, content: str, denied_selector: str) -> None:
        self._content = content
        self._denied_selector = denied_selector

    def goto(self, *_args: object, **_kwargs: object) -> None:
        return None

    def content(self) -> str:
        return self._content

    def wait_for_selector(self, selector: str, **_kwargs: object) -> None:
        if selector == self._denied_selector:
            raise CloakBrowserLicenseError(
                "CloakBrowser Pro: license key is invalid, expired, or missing. "
                "Check CLOAKBROWSER_LICENSE_KEY."
            )


@pytest.mark.parametrize(
    ("content", "denied_selector", "prepare"),
    [
        (
            '<div class="appRow"><a class="fontBlack" href="/release"></a></div>',
            ".variants-table, .downloadButton",
            "release",
        ),
        ('<table class="variants-table"></table>', ".downloadButton", "variant"),
        ("", "#download-link", "download_link"),
    ],
)
def test_late_selector_denials_reach_worker_boundary(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    denied_selector: str,
    prepare: str,
) -> None:
    page = DeniedSelectorPage(content, denied_selector)
    browser = FakeBrowser()
    monkeypatch.setattr(FakeBrowser, "new_page", lambda _self: page)
    monkeypatch.setattr("src.apkmirror_downloader.launch", lambda **_kwargs: browser)
    scraper = ApkMirror_Downloader(license_key="configured-license")
    if prepare == "release":
        row = HTMLParser('<a class="fontBlack" href="/release"></a>').css_first("a")
        monkeypatch.setattr(scraper, "_extract_first_valid_row", lambda *_args: row)
    if prepare == "variant":
        monkeypatch.setattr(
            scraper,
            "_get_best_variant",
            lambda *_args, **_kwargs: "/variant",
        )

    with pytest.raises(CloakBrowserLicenseError):
        if prepare == "download_link":
            scraper._download_binary_via_browser(
                "https://www.apkmirror.com/download", "/tmp/fake.apk"
            )
        else:
            scraper.search("com.example")

    assert scraper.license_key_rejected is True
    assert browser.closed == 0


def test_static_download_code77_is_not_treated_as_network_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class NoFallbackDownloader(ApkMirror_Downloader):
        def _download_binary_via_browser(
            self, _target_url: str, _dest_path: str
        ) -> str | None:
            pytest.fail("must not fall back after a license denial")

    scraper = NoFallbackDownloader.__new__(NoFallbackDownloader)
    scraper._license_key = "configured-license"
    scraper.license_key_rejected = False

    def denied_urlopen(*_args: object, **_kwargs: object) -> object:
        raise CloakBrowserLicenseError(
            "CloakBrowser Pro: license key is invalid, expired, or missing. "
            "Check CLOAKBROWSER_LICENSE_KEY."
        )

    monkeypatch.setattr(
        "src.apkmirror_downloader.urllib.request.urlopen", denied_urlopen
    )

    with pytest.raises(CloakBrowserLicenseError):
        scraper.download_file("https://example.invalid/file", str(tmp_path), "file.apk")


def test_extract_app_metadata_propagates_scraper_license_denial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(patcher, "DATA_DIR", tmp_path)

    class DeniedScraper:
        def search(self, **_kwargs: object) -> object:
            raise CloakBrowserLicenseError(
                "CloakBrowser Pro: license key is invalid, expired, or missing. "
                "Check CLOAKBROWSER_LICENSE_KEY."
            )

    with pytest.raises(CloakBrowserLicenseError):
        patcher.extract_app_metadata(
            tmp_path / "app.apk", "com.example", DeniedScraper()
        )


def test_build_code77_reaches_worker_and_releases_build_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakeBrowser()

    class FakeDownloader:
        license_key_rejected = False

        def close(self) -> None:
            browser.close()

    worker = main.ScraperWorker(launch=lambda: FakeDownloader())
    worker.start()
    assert worker.initialized.wait(5)
    monkeypatch.setattr(
        patcher,
        "run_pipeline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            CloakBrowserLicenseError(
                "CloakBrowser Pro: license key is invalid, expired, or missing. "
                "Check CLOAKBROWSER_LICENSE_KEY."
            )
        ),
    )
    monkeypatch.setattr(main, "SCRAPER_WORKER", worker)
    monkeypatch.setattr(main, "send_notification", lambda *_args: None)

    main.build_task(package_ids=[], is_cron=False)
    status = worker.status()
    worker.shutdown()
    worker.join(5)

    assert browser.closed == 1
    assert status["license_key_rejected"] is True
    assert main.BUILD_STATUS["in_progress"] is False
    assert str(main.BUILD_STATUS["message"]).startswith("Failed:")


def test_refresh_versions_degrades_after_worker_closes_denied_scraper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakeBrowser()

    class DeniedScraper:
        def latest_version(self, *_args: object, **_kwargs: object) -> str:
            raise CloakBrowserLicenseError(
                "CloakBrowser Pro: license key is invalid, expired, or missing. "
                "Check CLOAKBROWSER_LICENSE_KEY."
            )

        def close(self) -> None:
            browser.close()

    worker = main.ScraperWorker(launch=lambda: DeniedScraper())
    worker.start()
    assert worker.initialized.wait(5)
    monkeypatch.setattr(main, "SCRAPER_WORKER", worker)
    monkeypatch.setattr(
        main,
        "load_config",
        lambda: {"apps": [{"id": "com.example", "version": ""}]},
    )
    monkeypatch.setattr(main.time, "sleep", lambda _delay: None)

    main.refresh_versions_task()
    status = worker.status()
    worker.shutdown()
    worker.join(5)

    assert browser.closed == 1
    assert status["license_key_rejected"] is True
    assert main.SCRAPER_ACTIVE is False


def test_downloader_omits_license_argument_when_key_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    browser = FakeBrowser()

    def launch(**kwargs: object) -> FakeBrowser:
        calls.append(kwargs)
        return browser

    monkeypatch.setattr("src.apkmirror_downloader.launch", launch)

    downloader = ApkMirror_Downloader()

    assert calls == [{}]
    assert downloader.license_key_rejected is False


def test_startup_cache_isolates_library_cached_credentials(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    old_home = tmp_path / "old-home"
    old_cache = old_home / ".cloakbrowser"
    old_cache.mkdir(parents=True)
    (old_cache / "license.key").write_text("test-only-cached-key")
    monkeypatch.setenv("HOME", str(old_home))
    monkeypatch.delenv("CLOAKBROWSER_LICENSE_KEY", raising=False)

    main.configure_cloakbrowser_cache()

    cache_dir = Path(os.environ["CLOAKBROWSER_CACHE_DIR"])
    assert cache_dir == data_dir / "bin" / "cloakbrowser"
    assert resolve_license_key() is None


def test_cache_configuration_refuses_a_cached_library_key(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main.configure_cloakbrowser_cache()
    cache_dir = data_dir / "bin" / "cloakbrowser"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "license.key").write_text("must-not-be-read")

    with pytest.raises(RuntimeError, match="contains an unmanaged licence key"):
        main.configure_cloakbrowser_cache()


def test_launch_refuses_a_cached_key_added_after_startup(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main.configure_cloakbrowser_cache()
    cache_dir = data_dir / "bin" / "cloakbrowser"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "license.key").write_text("test-only-cached-key")
    launched = False

    def launch(**kwargs: object) -> FakeBrowser:
        nonlocal launched
        launched = True
        return FakeBrowser()

    monkeypatch.setattr("src.apkmirror_downloader.launch", launch)

    with pytest.raises(RuntimeError, match="contains an unmanaged licence key"):
        ApkMirror_Downloader()

    assert launched is False


def test_keyless_launch_refuses_a_reintroduced_library_environment_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", "test-only-environment-key")
    launched = False

    def launch(**kwargs: object) -> FakeBrowser:
        nonlocal launched
        launched = True
        return FakeBrowser()

    monkeypatch.setattr("src.apkmirror_downloader.launch", launch)

    with pytest.raises(RuntimeError, match="keyless launch refused"):
        ApkMirror_Downloader()

    assert launched is False


@pytest.mark.parametrize(
    "message",
    [
        (
            "CloakBrowser Pro: session limit reached for your plan. Close another "
            "running session or upgrade your plan."
        ),
        (
            "CloakBrowser Pro: couldn't verify your license (license server "
            "unreachable or a connection problem)."
        ),
        (
            "CloakBrowser Pro: local configuration problem, ~/.cloakbrowser "
            "is not writable."
        ),
    ],
)
def test_non_77_post_handshake_denial_closes_without_fallback(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    browser = FakeBrowser()

    def denied_page(self: FakeBrowser) -> object:
        raise CloakBrowserLicenseError(message)

    launches: list[dict[str, object]] = []

    def launch(**kwargs: object) -> FakeBrowser:
        launches.append(kwargs)
        return browser

    monkeypatch.setattr(FakeBrowser, "new_page", denied_page)
    monkeypatch.setattr("src.apkmirror_downloader.launch", launch)

    with pytest.raises(CloakBrowserLicenseError):
        ApkMirror_Downloader(license_key="configured-license")

    assert browser.closed == 1
    assert launches == [{"license_key": "configured-license"}]


def test_post_handshake_code77_closes_browser_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    denied_browser = FakeBrowser()
    keyless_browser = FakeBrowser()
    launches: list[dict[str, object]] = []

    def denied_page(self: FakeBrowser) -> object:
        if self is denied_browser:
            raise CloakBrowserLicenseError(
                "CloakBrowser Pro: license key is invalid, expired, or missing. "
                "Check CLOAKBROWSER_LICENSE_KEY."
            )
        return object()

    def launch(**kwargs: object) -> FakeBrowser:
        launches.append(kwargs)
        return denied_browser if kwargs else keyless_browser

    monkeypatch.setattr(FakeBrowser, "new_page", denied_page)
    monkeypatch.setattr("src.apkmirror_downloader.launch", launch)

    downloader = ApkMirror_Downloader(license_key="configured-license")

    assert launches == [{"license_key": "configured-license"}, {}]
    assert denied_browser.closed == 1
    assert downloader.license_key_rejected is True


def test_late_code77_closes_browser_and_uses_keyless_until_key_rotates() -> None:
    effective_key = ["initial-license"]
    launch_keys: list[str | None] = []
    browsers: list[FakeBrowser] = []

    def launch(license_key: str | None) -> FakeBrowser:
        launch_keys.append(license_key)
        browser = FakeBrowser()
        browsers.append(browser)
        return browser

    worker = main.ScraperWorker(
        launch=launch,
        license_key_provider=lambda: effective_key[0],
    )
    worker.start()
    assert worker.initialized.wait(5)

    with pytest.raises(CloakBrowserLicenseError):
        worker.execute(
            lambda scraper: (_ for _ in ()).throw(
                CloakBrowserLicenseError(
                    "CloakBrowser Pro: license key is invalid, expired, or missing. "
                    "Check CLOAKBROWSER_LICENSE_KEY."
                )
            )
        )
    assert browsers[0].closed == 1
    assert worker.execute(lambda scraper: "keyless") == "keyless"
    assert worker.status()["license_key_rejected"] is True

    effective_key[0] = "rotated-license"
    assert worker.execute(lambda scraper: "rotated") == "rotated"
    status = worker.status()
    worker.shutdown()
    worker.join(5)

    assert launch_keys == ["initial-license", None, "rotated-license"]
    assert status["license_key_rejected"] is False


def test_search_denial_reaches_worker_and_failed_keyless_relaunch_keeps_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    launches: list[str | None] = []
    browsers: list[FakeBrowser] = []

    def launch(license_key: str | None = None) -> ApkMirror_Downloader:
        launches.append(license_key)
        if license_key is None and len(launches) > 1:
            raise RuntimeError("keyless launch unavailable")
        browser = FakeBrowser()
        browsers.append(browser)
        monkeypatch.setattr(
            "src.apkmirror_downloader.launch", lambda **_kwargs: browser
        )
        monkeypatch.setattr(
            FakeBrowser, "new_page", lambda _self: DeniedNavigationPage()
        )
        return ApkMirror_Downloader(license_key=license_key)

    worker = main.ScraperWorker(
        launch=launch,
        clock=lambda: now[0],
        cooldown=10,
        license_key_provider=lambda: "configured-license",
    )
    worker.start()
    assert worker.initialized.wait(5)

    with pytest.raises(CloakBrowserLicenseError):
        worker.execute(lambda scraper: scraper.search("com.example"))
    assert browsers[0].closed == 1
    assert worker.status()["license_key_rejected"] is True

    with pytest.raises(main.ScraperUnavailableError):
        worker.execute(lambda scraper: "unreachable")
    assert worker.status()["license_key_rejected"] is True
    now[0] = 11
    with pytest.raises(main.ScraperUnavailableError):
        worker.execute(lambda scraper: "unreachable")
    status = worker.status()
    worker.shutdown()
    worker.join(5)

    assert launches == ["configured-license", None, None]
    assert status["license_key_rejected"] is True


def test_downloader_passes_configured_key_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    browser = FakeBrowser()

    def launch(**kwargs: object) -> FakeBrowser:
        calls.append(kwargs)
        return browser

    monkeypatch.setattr("src.apkmirror_downloader.launch", launch)

    ApkMirror_Downloader(license_key="private-license")

    assert calls == [{"license_key": "private-license"}]


def test_worker_relaunches_at_task_boundary_when_effective_key_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key: list[str | None] = [None]
    launched: list[str | None] = []
    browsers: list[FakeBrowser] = []

    def launch(license_key: str | None) -> FakeBrowser:
        launched.append(license_key)
        browser = FakeBrowser()
        browsers.append(browser)
        return browser

    worker = main.ScraperWorker(
        launch=launch,
        license_key_provider=lambda: key[0],
    )
    worker.start()
    assert worker.initialized.wait(5)
    assert worker.execute(lambda scraper: "first") == "first"

    key[0] = "rotated-license"
    assert worker.execute(lambda scraper: "second") == "second"
    status = worker.status()
    worker.shutdown()
    worker.join(5)

    assert launched == [None, "rotated-license"]
    assert browsers[0].closed == 1
    assert status["license_key_rejected"] is False


def test_worker_status_exposes_rejection_without_the_key() -> None:
    key = "private-license-value"

    class RejectedBrowser(FakeBrowser):
        license_key_rejected = True

    worker = main.ScraperWorker(
        launch=lambda effective_key: RejectedBrowser(),
        license_key_provider=lambda: key,
    )
    worker.start()
    assert worker.initialized.wait(5)

    status = worker.status()
    worker.shutdown()
    worker.join(5)

    assert status["license_key_rejected"] is True
    assert key not in repr(status)


@pytest.mark.parametrize(
    "message,falls_back",
    [
        (
            "CloakBrowser Pro: license key is invalid, expired, or missing. Check CLOAKBROWSER_LICENSE_KEY.",
            True,
        ),
        ("CloakBrowser Pro: session limit reached for your plan.", False),
        ("CloakBrowser Pro: couldn't verify your license.", False),
        ("CloakBrowser Pro: local configuration problem.", False),
    ],
)
def test_only_invalid_key_denial_falls_back_keyless(
    monkeypatch: pytest.MonkeyPatch, message: str, falls_back: bool
) -> None:
    calls: list[dict[str, object]] = []
    browser = FakeBrowser()

    def launch(**kwargs: object) -> object:
        calls.append(kwargs)
        if len(calls) == 1:
            raise CloakBrowserLicenseError(message)
        return browser

    monkeypatch.setattr("src.apkmirror_downloader.launch", launch)

    if falls_back:
        downloader = ApkMirror_Downloader(license_key="private-license")
        assert calls == [{"license_key": "private-license"}, {}]
        assert downloader.license_key_rejected is True
    else:
        with pytest.raises(CloakBrowserLicenseError):
            ApkMirror_Downloader(license_key="private-license")
        assert calls == [{"license_key": "private-license"}]


def test_rejected_key_status_and_stdout_logs_never_expose_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = "private-license-value"
    monkeypatch.setattr(
        "src.apkmirror_downloader.launch",
        lambda **kwargs: (
            (_ for _ in ()).throw(
                CloakBrowserLicenseError(
                    "CloakBrowser Pro: license key is invalid, expired, or missing. "
                    "Check CLOAKBROWSER_LICENSE_KEY."
                )
            )
            if kwargs
            else FakeBrowser()
        ),
    )

    downloader = ApkMirror_Downloader(license_key=key)
    output = capsys.readouterr().out

    assert downloader.license_key_rejected is True
    assert "licence key rejected" in output
    assert key not in output
    assert not hasattr(downloader, "license_key")


def test_failed_keyless_fallback_has_fixed_error_and_rejection_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "private-license-value"

    def launch(**kwargs: object) -> object:
        if kwargs:
            raise CloakBrowserLicenseError(
                "CloakBrowser Pro: license key is invalid, expired, or missing. "
                "Check CLOAKBROWSER_LICENSE_KEY."
            )
        raise RuntimeError(f"keyless failure {key}")

    monkeypatch.setattr("src.apkmirror_downloader.launch", launch)
    with pytest.raises(LicenseFallbackError) as raised:
        ApkMirror_Downloader(license_key=key)

    assert key not in str(raised.value)
    assert "keyless launch failed" in str(raised.value)


def test_worker_reports_rejection_when_keyless_fallback_fails() -> None:
    worker = main.ScraperWorker(
        launch=lambda effective_key: (_ for _ in ()).throw(
            LicenseFallbackError("keyless launch failed")
        ),
        license_key_provider=lambda: "private-license-value",
    )
    worker.start()
    assert worker.initialized.wait(5)

    status = worker.status()
    worker.shutdown()
    worker.join(5)

    assert status["license_key_rejected"] is True
    assert "private-license-value" not in repr(status)
