"""Startup removes the retired cloakbrowser binary cache."""

from src import main


def test_removes_the_cache_and_a_key_file_inside_it(isolated_data_dir):
    cache = isolated_data_dir / "bin" / "cloakbrowser"
    (cache / "chromium-1").mkdir(parents=True)
    (cache / "chromium-1" / "chrome").write_bytes(b"binary")
    (cache / "license_key").write_text("fake-retired-key-0030")

    main.remove_legacy_cloakbrowser_cache()

    assert not cache.exists()


def test_leaves_the_rest_of_bin_alone(isolated_data_dir):
    (isolated_data_dir / "bin" / "cloakbrowser").mkdir(parents=True)
    tool = isolated_data_dir / "bin" / "tool.jar"
    tool.write_bytes(b"jar")

    main.remove_legacy_cloakbrowser_cache()

    assert tool.read_bytes() == b"jar"


def test_is_a_no_op_when_there_is_no_cache(isolated_data_dir):
    main.remove_legacy_cloakbrowser_cache()

    assert not (isolated_data_dir / "bin" / "cloakbrowser").exists()
