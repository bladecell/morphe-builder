"""Tests for the APKPure fallback used when APKMirror blocks the browser."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import patcher


def _completed(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)


def test_apkeep_fallback_returns_downloaded_file(tmp_path, monkeypatch):
    fake_binary = tmp_path / "apkeep"
    fake_binary.write_text("")
    monkeypatch.setattr(patcher.shutil, "which", lambda name: str(fake_binary))

    def fake_run(command, **kwargs):
        assert f"com.example@1.2.3" in command
        assert "apk-pure" in command
        (tmp_path / "out").mkdir()
        (tmp_path / "out" / "com.example@1.2.3.apk").write_bytes(b"apk")
        return _completed()

    monkeypatch.setattr(patcher, "run_child", fake_run)

    result = patcher.download_apk_via_apkeep(
        "com.example", "1.2.3", str(tmp_path / "out")
    )

    assert result == tmp_path / "out" / "com.example@1.2.3.apk"


def test_apkeep_fallback_returns_none_when_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(patcher.shutil, "which", lambda name: None)
    monkeypatch.setattr(patcher, "DATA_DIR", tmp_path)

    assert (
        patcher.download_apk_via_apkeep("com.example", "1.2.3", str(tmp_path)) is None
    )


def test_apkeep_fallback_returns_none_on_failed_download(tmp_path, monkeypatch):
    fake_binary = tmp_path / "apkeep"
    fake_binary.write_text("")
    monkeypatch.setattr(patcher.shutil, "which", lambda name: str(fake_binary))
    monkeypatch.setattr(
        patcher, "run_child", lambda *a, **k: _completed(1, "no such version")
    )

    assert patcher.download_apk_via_apkeep("com.example", "9", str(tmp_path)) is None


def test_download_apk_uses_apkeep_when_scraper_finds_nothing(tmp_path, monkeypatch):
    scraper = MagicMock()
    scraper.search.return_value = None
    scraper.download_from_uptodown.return_value = None
    monkeypatch.setattr(patcher, "get_app_preferences", lambda *a: None)
    monkeypatch.setattr(patcher, "extract_app_metadata", lambda *a, **k: None)
    monkeypatch.setattr(patcher, "DATA_DIR", tmp_path)
    monkeypatch.setattr(patcher, "_read_package_id", lambda *a, **k: "com.example")
    monkeypatch.setattr(
        patcher,
        "download_apk_via_apkeep",
        lambda package, version, directory: _write(Path(directory) / "x.apk"),
    )
    final_path = tmp_path / "final.apk"

    patcher.download_apk("com.example", "1.2.3", final_path, {}, {}, scraper=scraper)

    assert final_path.read_bytes() == b"apk"
    scraper.download_file.assert_not_called()


def test_download_apk_rejects_apkeep_file_with_wrong_package(tmp_path, monkeypatch):
    scraper = MagicMock()
    scraper.search.return_value = None
    scraper.download_from_uptodown.return_value = None
    monkeypatch.setattr(patcher, "get_app_preferences", lambda *a: None)
    monkeypatch.setattr(patcher, "DATA_DIR", tmp_path)
    monkeypatch.setattr(patcher, "_read_package_id", lambda *a, **k: "com.other")
    monkeypatch.setattr(
        patcher,
        "download_apk_via_apkeep",
        lambda package, version, directory: _write(Path(directory) / "x.apk"),
    )
    final_path = tmp_path / "final.apk"

    with pytest.raises(Exception, match="does not verifiably contain package"):
        patcher.download_apk(
            "com.example", "1.2.3", final_path, {}, {}, scraper=scraper
        )

    assert not final_path.exists()


def test_download_apk_raises_when_both_sources_fail(tmp_path, monkeypatch):
    scraper = MagicMock()
    scraper.search.return_value = None
    scraper.download_from_uptodown.return_value = None
    monkeypatch.setattr(patcher, "get_app_preferences", lambda *a: None)
    monkeypatch.setattr(patcher, "download_apk_via_apkeep", lambda *a: None)

    with pytest.raises(Exception, match="APKMirror, Uptodown or APKPure"):
        patcher.download_apk(
            "com.example", "1.2.3", tmp_path / "final.apk", {}, {}, scraper=scraper
        )


def _write(path: Path) -> Path:
    path.write_bytes(b"apk")
    return path
