"""Metadata extraction and upload behavior when optional icons are unavailable."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import main, patcher
from src.apkmirror_downloader import license_error_for_code

PACKAGE_ID = "com.example.app"
APK_METADATA = "AppName=Example App\nVersionName=9.8.7\n"


class FakeScraper:
    def __init__(self, result=None, download_error: Exception | None = None):
        self.result = result
        self.download_error = download_error
        self.download_calls: list[dict[str, str]] = []

    def search(self, **_kwargs: object) -> object:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def download_file(self, **kwargs: str) -> str:
        self.download_calls.append(kwargs)
        if self.download_error:
            raise self.download_error
        return str(Path(kwargs["dest_directory"]) / kwargs["filename"])

    def close(self) -> None:
        pass


def prepare_apkeditor(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    apkeditor = data_dir / "bin" / "APKEditor.jar"
    apkeditor.parent.mkdir(parents=True, exist_ok=True)
    apkeditor.touch()
    monkeypatch.setattr(
        patcher,
        "run_child",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=APK_METADATA),
    )


@pytest.mark.parametrize(
    "search_result,download_error",
    [
        (None, None),
        (SimpleNamespace(icon_url="Unknown"), None),
        (
            SimpleNamespace(icon_url="https://invalid.example/icon.png"),
            OSError("icon unavailable"),
        ),
    ],
)
def test_missing_icon_does_not_discard_extracted_metadata(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    search_result: object,
    download_error: Exception | None,
) -> None:
    prepare_apkeditor(data_dir, monkeypatch)
    scraper = FakeScraper(search_result, download_error)

    result = patcher.extract_app_metadata(data_dir / "app.apk", PACKAGE_ID, scraper)

    expected = {"real_name": "Example App", "version": "9.8.7"}
    assert result == expected
    assert json.loads((data_dir / "bin" / "metadata.json").read_text()) == {
        PACKAGE_ID: expected
    }


def test_existing_icon_is_preserved_without_scraper_lookup(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_apkeditor(data_dir, monkeypatch)
    icon = data_dir / "apks" / "icons" / f"{PACKAGE_ID}.png"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"existing icon")
    scraper = FakeScraper(RuntimeError("must not look up icon"))

    metadata = patcher.extract_app_metadata(data_dir / "app.apk", PACKAGE_ID, scraper)

    assert metadata == {"real_name": "Example App", "version": "9.8.7"}
    assert icon.read_bytes() == b"existing icon"


def test_partial_icon_download_is_removed_and_later_retry_can_replace_it(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_apkeditor(data_dir, monkeypatch)

    class PartialThenSuccessScraper(FakeScraper):
        attempts = 0

        def search(self, **_kwargs: object) -> object:
            return SimpleNamespace(icon_url="https://invalid.example/icon.png")

        def download_file(self, **kwargs: str) -> str:
            self.attempts += 1
            path = Path(kwargs["dest_directory"]) / kwargs["filename"]
            path.write_bytes(b"partial" if self.attempts == 1 else b"complete")
            if self.attempts == 1:
                raise OSError("connection interrupted")
            return str(path)

    scraper = PartialThenSuccessScraper()
    icon = data_dir / "apks" / "icons" / f"{PACKAGE_ID}.png"
    expected = {"real_name": "Example App", "version": "9.8.7"}

    assert (
        patcher.extract_app_metadata(data_dir / "app.apk", PACKAGE_ID, scraper)
        == expected
    )
    assert not icon.exists()
    assert list(icon.parent.iterdir()) == []

    assert (
        patcher.extract_app_metadata(data_dir / "app.apk", PACKAGE_ID, scraper)
        == expected
    )
    assert icon.read_bytes() == b"complete"
    assert list(icon.parent.iterdir()) == [icon]


def test_license_denial_while_fetching_icon_still_propagates(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_apkeditor(data_dir, monkeypatch)
    denied = license_error_for_code(77)
    assert denied is not None
    scraper = FakeScraper(denied)

    with pytest.raises(type(denied)):
        patcher.extract_app_metadata(data_dir / "app.apk", PACKAGE_ID, scraper)

    assert not (data_dir / "bin" / "metadata.json").exists()


def test_upload_persists_apk_and_extracted_metadata(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_apkeditor(data_dir, monkeypatch)
    monkeypatch.setattr(
        patcher, "ApkMirror_Downloader", lambda **_kwargs: FakeScraper(None)
    )
    from fastapi.testclient import TestClient

    result = TestClient(main.app).post(
        "/api/ui/upload_apk",
        data={"package_id": PACKAGE_ID, "version_str": "manual 4.2"},
        files={"file": ("upload.apk", b"apk payload")},
    )

    assert result.status_code == 200
    assert (
        data_dir / "apks" / "raw" / f"{PACKAGE_ID}-manual_4.2.apk"
    ).read_bytes() == b"apk payload"
    assert json.loads((data_dir / "bin" / "metadata.json").read_text()) == {
        PACKAGE_ID: {"real_name": "Example App", "version": "9.8.7"}
    }
