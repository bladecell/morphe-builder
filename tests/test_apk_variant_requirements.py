"""HushFacebook's Android floor applies only to its resolved Facebook patches."""

import json
from pathlib import Path

import pytest
from selectolax.parser import HTMLParser

from src import patcher
from src.apkmirror_downloader import AndroidVariantMetadataError

FACEBOOK = "com.facebook.katana"
HUSH = "SysAdminDoc/HushFacebook"


def write_source(data_dir, repo: str, name: str) -> None:
    paths = patcher.get_source_paths(repo)
    paths[".json"].parent.mkdir(parents=True, exist_ok=True)
    paths[".json"].write_text(
        json.dumps(
            {
                "patches": [
                    {
                        "name": name,
                        "compatiblePackages": {FACEBOOK: ["1.0"]},
                    }
                ]
            }
        )
    )
    paths[".mpp"].write_bytes(b"bundle")


def test_hushfacebook_floor_uses_casefolded_resolved_repo_identity(data_dir):
    write_source(data_dir, "sysadmindoc/hushfacebook", "Hide ads")
    app = {
        "id": FACEBOOK,
        "include_patches": {"SYSADMINDOC/HUSHFACEBOOK": ["Hide ads"]},
    }
    config = {"sources": [{"repo": "sysadmindoc/hushfacebook", "active": True}]}

    assert patcher.required_minimum_sdk(app, config) == 30


def test_unselected_or_inactive_hushfacebook_does_not_apply_floor(data_dir):
    write_source(data_dir, HUSH, "Hide ads")
    config = {"sources": [{"repo": HUSH, "active": True}]}
    unselected = {"id": FACEBOOK, "include_patches": []}
    inactive = {
        "id": FACEBOOK,
        "include_patches": {HUSH: ["Hide ads"]},
    }

    assert patcher.required_minimum_sdk(unselected, config) is None
    assert (
        patcher.required_minimum_sdk(
            inactive, {"sources": [{"repo": HUSH, "active": False}]}
        )
        is None
    )


def test_legacy_selection_applies_floor_only_when_resolved_to_hush(data_dir):
    write_source(data_dir, "other/facebook", "Hide ads")
    write_source(data_dir, HUSH, "Hide ads")
    app = {"id": FACEBOOK, "include_patches": ["Hide ads"]}

    assert (
        patcher.required_minimum_sdk(
            app,
            {
                "sources": [
                    {"repo": "other/facebook", "active": True},
                    {"repo": HUSH, "active": True},
                ]
            },
        )
        is None
    )
    assert (
        patcher.required_minimum_sdk(
            app,
            {
                "sources": [
                    {"repo": HUSH, "active": True},
                    {"repo": "other/facebook", "active": True},
                ]
            },
        )
        == 30
    )


def test_other_apps_never_receive_the_hushfacebook_floor(data_dir):
    write_source(data_dir, HUSH, "Hide ads")

    assert (
        patcher.required_minimum_sdk(
            {"id": "com.example.other", "include_patches": {HUSH: ["Hide ads"]}},
            {"sources": [{"repo": HUSH, "active": True}]},
        )
        is None
    )


def variant_row(path: str, android: str, bundle: bool) -> str:
    badge = '<span class="apkm-badge">BUNDLE</span>' if bundle else ""
    return f"""<tr class="table-row">
      <td class="table-cell"><a class="accent_color" href="{path}">APK</a>{badge}</td>
      <td class="table-cell">arm64-v8a</td>
      <td class="table-cell">{android}</td>
      <td class="table-cell">nodpi</td>
    </tr>"""


def test_variant_floor_filters_old_android_before_bundle_and_arch_scoring():
    html = (
        '<table class="variants-table">'
        + variant_row("/android-9", "Android 9+", bundle=True)
        + variant_row("/android-11", "Android 11+", bundle=False)
        + "</table>"
    )
    scraper = patcher.ApkMirror_Downloader.__new__(patcher.ApkMirror_Downloader)

    selected = scraper._get_best_variant(
        HTMLParser(html), "arm64-v8a", "nodpi", True, minimum_sdk=30
    )

    assert selected.endswith("/android-11")


def test_variant_floor_accepts_apkmirror_major_zero_labels_and_skips_bad_rows():
    html = (
        '<table class="variants-table">'
        + variant_row("/android-9", "Android 9.0+", bundle=True)
        + variant_row("/android-11", "Android 11+", bundle=False)
        + variant_row("/android-8", "Android 8.0+", bundle=False)
        + "</table>"
    )
    scraper = patcher.ApkMirror_Downloader.__new__(patcher.ApkMirror_Downloader)

    selected = scraper._get_best_variant(
        HTMLParser(html), "arm64-v8a", "nodpi", True, minimum_sdk=30
    )

    assert selected.endswith("/android-11")


def test_unrecognized_row_does_not_block_another_verified_eligible_row():
    html = (
        '<table class="variants-table">'
        + variant_row("/unknown", "Android 99+", bundle=False)
        + variant_row("/android-11", "Android 11+", bundle=False)
        + "</table>"
    )
    scraper = patcher.ApkMirror_Downloader.__new__(patcher.ApkMirror_Downloader)

    selected = scraper._get_best_variant(
        HTMLParser(html), "arm64-v8a", "nodpi", False, minimum_sdk=30
    )

    assert selected.endswith("/android-11")


def test_constrained_selection_returns_no_candidate_if_android_floor_is_unverified():
    html = (
        '<table class="variants-table">'
        + variant_row("/unknown", "Android 99+", bundle=False)
        + "</table>"
    )
    scraper = patcher.ApkMirror_Downloader.__new__(patcher.ApkMirror_Downloader)

    assert (
        scraper._get_best_variant(
            HTMLParser(html), "arm64-v8a", "nodpi", False, minimum_sdk=30
        )
        is None
    )


@pytest.mark.parametrize(
    ("label", "expected_api"),
    [
        ("Android 9.0+", 28),
        ("Android 8.0+", 26),
        ("Android 11.0+", 30),
        ("Android 4.0+", 14),
        ("Android 4.4+", 19),
        ("Android 8.1+", 27),
        ("Android 12L+", 32),
    ],
)
def test_major_zero_android_labels_map_to_known_release_api(label, expected_api):
    from src.apkmirror_downloader import _android_minimum_api

    assert _android_minimum_api(label) == expected_api


def test_android_label_with_unknown_minor_is_not_truncated_to_major():
    from src.apkmirror_downloader import (
        AndroidVariantMetadataError,
        _android_minimum_api,
    )

    with pytest.raises(AndroidVariantMetadataError):
        _android_minimum_api("Android 9.1+")


@pytest.mark.parametrize("label", ["", "Android 9+ Android 11+", "Android 99+"])
def test_variant_floor_skips_missing_or_ambiguous_android_labels(label):
    row = variant_row("/candidate", label, bundle=False)
    scraper = patcher.ApkMirror_Downloader.__new__(patcher.ApkMirror_Downloader)

    assert (
        scraper._get_best_variant(
            HTMLParser(f'<table class="variants-table">{row}</table>'),
            "arm64-v8a",
            "nodpi",
            False,
            minimum_sdk=30,
        )
        is None
    )


def test_minimum_sdk_inspector_reads_explicit_value_and_fails_closed(monkeypatch):
    class Result:
        returncode = 0
        stdout = '[{"MinSdkVersion": 30}]'

    monkeypatch.setattr(patcher, "run_child", lambda *args, **kwargs: Result())
    for sdk in (30, 31):
        Result.stdout = json.dumps([{"MinSdkVersion": sdk}])
        assert patcher._read_minimum_sdk("java", "/tmp/candidate.apk") == sdk

    for unsupported in (
        '[{"TargetSdkVersion": 35}]',
        '[{"MinSdkVersion": true}]',
        '[{"MinSdkVersion": 30.5}]',
        '[{"MinSdkVersion": 28}, {"MinSdkVersion": 30}]',
        "not JSON",
    ):
        Result.stdout = unsupported
        with pytest.raises(AndroidVariantMetadataError):
            patcher._read_minimum_sdk("java", "/tmp/candidate.apk")

    def inspector_unavailable(*_args, **_kwargs):
        raise FileNotFoundError("java")

    monkeypatch.setattr(patcher, "run_child", inspector_unavailable)
    with pytest.raises(patcher.AndroidVariantInspectionError):
        patcher._read_minimum_sdk("java", "/tmp/candidate.apk")


def test_installed_apkeditor_inspects_real_cached_facebook_apk(monkeypatch):
    project_root = Path(__file__).resolve().parents[1]
    apkeditor = project_root / "bin" / "APKEditor.jar"
    apk = project_root / "apks" / "raw" / "com.facebook.katana-581.0.0.45.58.apk"
    if not apkeditor.is_file() or not apk.is_file():
        pytest.skip("local APKEditor or Facebook APK fixture is not available")

    monkeypatch.setattr(patcher, "DATA_DIR", project_root)
    assert patcher._read_minimum_sdk("java", apk) == 28


def test_upload_rechecks_policy_at_atomic_promotion(data_dir, monkeypatch):
    from fastapi.testclient import TestClient

    from src import config_store, main

    source = patcher.get_source_paths(HUSH)
    source[".json"].parent.mkdir(parents=True, exist_ok=True)
    source[".json"].write_text(
        json.dumps(
            {"patches": [{"name": "Hide ads", "compatiblePackages": {FACEBOOK: []}}]}
        )
    )
    source[".mpp"].write_bytes(b"bundle")

    def add_facebook_app(config):
        config["sources"].append({"repo": HUSH, "active": True})
        config["apps"].append({"id": FACEBOOK, "include_patches": []})

    config_store.update_config(add_facebook_app)

    def select_hush(config):
        next(app for app in config["apps"] if app["id"] == FACEBOOK).update(
            include_patches={HUSH: ["Hide ads"]}
        )

    config_store.update_config(select_hush)
    config = config_store.load_config()
    assert (
        patcher.required_minimum_sdk(
            next(app for app in config["apps"] if app["id"] == FACEBOOK), config
        )
        == 30
    )
    config_store.update_config(
        lambda current: next(
            app for app in current["apps"] if app["id"] == FACEBOOK
        ).update(include_patches=[])
    )
    config = config_store.load_config()
    assert (
        patcher.required_minimum_sdk(
            next(app for app in config["apps"] if app["id"] == FACEBOOK), config
        )
        is None
    )
    raw = data_dir / "apks" / "raw" / f"{FACEBOOK}-1.apk"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"previous raw")
    patched = data_dir / "apks" / "patched" / f"{FACEBOOK}-previous.apk"
    patched.parent.mkdir(parents=True)
    patched.write_bytes(b"previous patched")

    real_update_config = main.update_config

    def enable_hush_before_promotion(mutate):
        config_store.update_config(
            lambda config: next(
                app for app in config["apps"] if app["id"] == FACEBOOK
            ).update(include_patches={HUSH: ["Hide ads"]})
        )
        return real_update_config(mutate)

    monkeypatch.setattr(main, "update_config", enable_hush_before_promotion)
    monkeypatch.setattr(patcher, "extract_app_metadata", lambda *_args, **_kwargs: None)
    response = TestClient(main.app).post(
        "/api/ui/upload_apk",
        data={"package_id": FACEBOOK, "version_str": "1"},
        files={"file": ("upload.apk", b"new raw")},
    )

    assert response.status_code == 400
    assert raw.read_bytes() == b"previous raw"
    assert patched.read_bytes() == b"previous patched"
    assert not list(raw.parent.glob("*.tmp"))


def test_upload_inspector_failure_is_safe_and_preserves_existing_apks(
    data_dir, monkeypatch
):
    from fastapi.testclient import TestClient

    from src import config_store, main

    source = patcher.get_source_paths(HUSH)
    source[".json"].parent.mkdir(parents=True, exist_ok=True)
    source[".json"].write_text(
        json.dumps(
            {"patches": [{"name": "Hide ads", "compatiblePackages": {FACEBOOK: []}}]}
        )
    )
    source[".mpp"].write_bytes(b"bundle")

    def configure(config):
        config["sources"] = [{"repo": HUSH, "active": True}]
        config["apps"] = [{"id": FACEBOOK, "include_patches": {HUSH: ["Hide ads"]}}]

    config_store.update_config(configure)
    raw = data_dir / "apks" / "raw" / f"{FACEBOOK}-1.apk"
    patched = data_dir / "apks" / "patched" / f"{FACEBOOK}-previous.apk"
    raw.parent.mkdir(parents=True)
    patched.parent.mkdir(parents=True)
    raw.write_bytes(b"previous raw")
    patched.write_bytes(b"previous patched")

    def inspector_unavailable(*_args):
        raise patcher.AndroidVariantInspectionError("java unavailable")

    monkeypatch.setattr(patcher, "_read_minimum_sdk", inspector_unavailable)
    monkeypatch.setattr(patcher, "extract_app_metadata", lambda *_args, **_kwargs: None)
    response = TestClient(main.app, raise_server_exceptions=False).post(
        "/api/ui/upload_apk",
        data={"package_id": FACEBOOK, "version_str": "1"},
        files={"file": ("upload.apk", b"candidate")},
    )

    assert response.status_code == 400
    assert "java unavailable" not in response.text
    assert raw.read_bytes() == b"previous raw"
    assert patched.read_bytes() == b"previous patched"
    assert not list(raw.parent.glob(".*.tmp"))


def test_download_rejects_old_merged_artifact_without_replacing_cache(
    data_dir, monkeypatch
):
    import zipfile
    from types import SimpleNamespace

    final_path = data_dir / "apks" / "raw" / f"{FACEBOOK}-1.apk"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"previous raw")

    class Scraper:
        def search(self, **_kwargs):
            return SimpleNamespace(
                download_url="https://example.invalid/bundle", icon_url="Unknown"
            )

        def download_file(self, target_url, dest_directory, filename):
            destination = Path(dest_directory) / filename
            with zipfile.ZipFile(destination, "w") as bundle:
                bundle.writestr("base.apk", b"split")
            return str(destination)

    def merge(_command, **_kwargs):
        output_path = Path(_command[_command.index("-o") + 1])
        output_path.write_bytes(b"merged Android 9 artifact")

    monkeypatch.setattr(patcher, "run_child", merge)
    monkeypatch.setattr(patcher, "_read_minimum_sdk", lambda *_args: 28)

    with pytest.raises(patcher.AndroidVariantUnavailableError):
        patcher.download_apk(
            FACEBOOK,
            "1",
            final_path,
            {"apkeditor_jar": "bin/APKEditor.jar"},
            {"settings": {}},
            scraper=Scraper(),
            minimum_sdk=30,
        )

    assert final_path.read_bytes() == b"previous raw"
    assert list(final_path.parent.iterdir()) == [final_path]
