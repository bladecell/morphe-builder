"""Tests for the Uptodown source: version check, download guards, ordering."""

import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import apkmirror_downloader as downloader
from src import patcher

PAGE_URL = "https://flashscore.en.uptodown.com/android/download/1223075994-x"
FILE_URL = "https://dw.uptodown.net/dwn/token/l77__x"
RESOLVED = downloader.ResolvedVariant(downloader.VariantSource.UPTODOWN, PAGE_URL)


def _page_html(version: str) -> str:
    return (
        '<html><div class="info"><a><h1 id="detail-app-name">App</h1></a>'
        f'<div class="version">{version}</div></div>'
        '<section><div class="version">26.10.1</div></section></html>'
    )


def _make_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("AndroidManifest.xml", "x")


def _downloader(
    monkeypatch,
    tmp_path,
    *,
    version="26.10.2",
    url=FILE_URL,
    filename="app-26-10-2.xapk",
    resolved=RESOLVED,
    page_url=PAGE_URL,
):
    instance = object.__new__(downloader.ApkMirror_Downloader)
    monkeypatch.setattr(downloader, "resolve_variant_url", lambda *a: resolved)
    instance._goto = MagicMock()
    instance.page = MagicMock()
    instance.page.url = page_url
    instance.page.content.return_value = _page_html(version)
    download = MagicMock()
    download.url = url
    download.suggested_filename = filename
    download.save_as.side_effect = lambda target: _make_zip(Path(target))
    instance.page.expect_download.return_value.__enter__.return_value.value = download
    return instance, download


def _fetch(instance, tmp_path):
    return instance.download_from_uptodown("eu.app", "26.10.2", str(tmp_path))


def test_downloads_xapk_when_page_version_matches(monkeypatch, tmp_path):
    instance, _ = _downloader(monkeypatch, tmp_path)

    path = _fetch(instance, tmp_path)

    assert path is not None
    assert path.suffix == ".xapk"
    assert zipfile.is_zipfile(path)
    instance._goto.assert_called_once_with(PAGE_URL)


def test_keeps_apk_extension(monkeypatch, tmp_path):
    instance, _ = _downloader(monkeypatch, tmp_path, filename="app.apk")

    path = _fetch(instance, tmp_path)

    assert path is not None
    assert path.suffix == ".apk"


def test_refuses_other_version_without_clicking(monkeypatch, tmp_path):
    instance, _ = _downloader(monkeypatch, tmp_path, version="26.10.3")

    assert _fetch(instance, tmp_path) is None
    instance.page.expect_download.assert_not_called()


def test_version_match_is_exact(monkeypatch, tmp_path):
    instance, _ = _downloader(monkeypatch, tmp_path, version="26.10.20")

    assert _fetch(instance, tmp_path) is None


def test_no_uptodown_resolution_returns_none(monkeypatch, tmp_path):
    apkmirror = downloader.ResolvedVariant(
        downloader.VariantSource.APKMIRROR, "https://www.apkmirror.com/x/"
    )
    for resolved in (None, apkmirror):
        instance, _ = _downloader(monkeypatch, tmp_path, resolved=resolved)

        assert _fetch(instance, tmp_path) is None
        instance._goto.assert_not_called()


def test_refuses_page_that_redirected_off_uptodown(monkeypatch, tmp_path):
    instance, _ = _downloader(
        monkeypatch, tmp_path, page_url="https://evil.test/android/download/1-x"
    )

    assert _fetch(instance, tmp_path) is None
    instance.page.expect_download.assert_not_called()


@pytest.mark.parametrize(
    "file_url",
    [
        "https://evil.test/a.apk",
        "http://dw.uptodown.net/a",
        "https://uptodown.net.evil.test/a",
    ],
)
def test_refuses_download_from_foreign_host(monkeypatch, tmp_path, file_url):
    instance, _ = _downloader(monkeypatch, tmp_path, url=file_url)

    assert _fetch(instance, tmp_path) is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("filename", ["payload.exe", "app.zip", "noext"])
def test_refuses_unexpected_file_type(monkeypatch, tmp_path, filename):
    instance, download = _downloader(monkeypatch, tmp_path, filename=filename)

    assert _fetch(instance, tmp_path) is None
    download.save_as.assert_not_called()


def test_rejects_and_removes_non_zip_payload(monkeypatch, tmp_path):
    instance, download = _downloader(monkeypatch, tmp_path)
    download.save_as.side_effect = lambda target: Path(target).write_text("<html>")

    assert _fetch(instance, tmp_path) is None
    assert not list(tmp_path.iterdir())


def test_browser_failure_returns_none(monkeypatch, tmp_path):
    instance, _ = _downloader(monkeypatch, tmp_path)
    instance._goto.side_effect = downloader.PlaywrightError("boom")

    assert _fetch(instance, tmp_path) is None


# download_apk ordering


def _scraper(uptodown_path: Path | None) -> MagicMock:
    scraper = MagicMock()
    scraper.search.return_value = None
    scraper.download_from_uptodown.return_value = uptodown_path
    return scraper


def _patch_pipeline(monkeypatch, tmp_path, apkeep=None, package_id="com.example"):
    monkeypatch.setattr(patcher, "get_app_preferences", lambda *a: None)
    monkeypatch.setattr(patcher, "extract_app_metadata", lambda *a, **k: None)
    monkeypatch.setattr(patcher, "DATA_DIR", tmp_path)
    monkeypatch.setattr(patcher, "_read_package_id", lambda *a, **k: package_id)
    monkeypatch.setattr(
        patcher, "download_apk_via_apkeep", apkeep or MagicMock(return_value=None)
    )


def _uptodown_file(tmp_path: Path) -> Path:
    path = tmp_path / "u.apk"
    path.write_bytes(b"uptodown")
    return path


def test_download_apk_uses_uptodown_before_apkeep(tmp_path, monkeypatch, capsys):
    apkeep = MagicMock()
    _patch_pipeline(monkeypatch, tmp_path, apkeep)
    final_path = tmp_path / "final.apk"

    patcher.download_apk(
        "com.example",
        "1.2.3",
        final_path,
        {},
        {},
        scraper=_scraper(_uptodown_file(tmp_path)),
    )

    assert final_path.read_bytes() == b"uptodown"
    apkeep.assert_not_called()
    assert "Uptodown" in capsys.readouterr().out


def test_download_apk_skips_uptodown_when_apkmirror_works(tmp_path, monkeypatch):
    _patch_pipeline(monkeypatch, tmp_path)
    scraper = _scraper(None)
    scraper.search.return_value = MagicMock(icon_url="Unknown")
    scraper.download_file.return_value = str(_uptodown_file(tmp_path))

    patcher.download_apk(
        "com.example", "1.2.3", tmp_path / "final.apk", {}, {}, scraper=scraper
    )

    scraper.download_from_uptodown.assert_not_called()


def test_download_apk_falls_back_to_apkeep_after_uptodown(tmp_path, monkeypatch):
    apkeep = MagicMock(
        side_effect=lambda package, version, directory: _write(
            Path(directory) / "x.apk"
        )
    )
    _patch_pipeline(monkeypatch, tmp_path, apkeep)
    final_path = tmp_path / "final.apk"

    patcher.download_apk(
        "com.example", "1.2.3", final_path, {}, {}, scraper=_scraper(None)
    )

    assert final_path.read_bytes() == b"apk"
    apkeep.assert_called_once()


def test_download_apk_names_all_sources_when_every_one_fails(tmp_path, monkeypatch):
    _patch_pipeline(monkeypatch, tmp_path)

    with pytest.raises(Exception, match="APKMirror, Uptodown or APKPure"):
        patcher.download_apk(
            "com.example",
            "1.2.3",
            tmp_path / "final.apk",
            {},
            {},
            scraper=_scraper(None),
        )


def test_download_apk_rejects_uptodown_file_with_other_package(tmp_path, monkeypatch):
    _patch_pipeline(monkeypatch, tmp_path, package_id="com.other")
    final_path = tmp_path / "final.apk"

    with pytest.raises(Exception, match="package"):
        patcher.download_apk(
            "com.example",
            "1.2.3",
            final_path,
            {},
            {},
            scraper=_scraper(_uptodown_file(tmp_path)),
        )

    assert not final_path.exists()


def test_download_apk_rejects_uptodown_file_with_unreadable_package(
    tmp_path, monkeypatch
):
    _patch_pipeline(monkeypatch, tmp_path, package_id="")

    with pytest.raises(Exception, match="package"):
        patcher.download_apk(
            "com.example",
            "1.2.3",
            tmp_path / "final.apk",
            {},
            {},
            scraper=_scraper(_uptodown_file(tmp_path)),
        )


def _write(path: Path) -> Path:
    path.write_bytes(b"apk")
    return path
