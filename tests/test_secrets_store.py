"""Tests for the DATA_DIR/.env secrets store."""

import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src import config_store, secrets_store

FAKE_TOKEN = "fake-token-for-tests-0001"
FAKE_KEY = "fake-env-token-for-tests-0002"
GITHUB = secrets_store.GITHUB_TOKEN


def env_file(data_dir):
    return data_dir / ".env"


def test_set_get_clear_round_trip(isolated_data_dir):
    assert secrets_store.get_secret(GITHUB) is None

    secrets_store.set_secret(GITHUB, FAKE_TOKEN)
    assert secrets_store.get_secret(GITHUB) == FAKE_TOKEN
    assert secrets_store.secret_source(GITHUB) == "file"

    secrets_store.clear_secret(GITHUB)
    assert secrets_store.get_secret(GITHUB) is None
    assert secrets_store.secret_source(GITHUB) is None
    assert FAKE_TOKEN not in env_file(isolated_data_dir).read_text()


def test_file_mode_is_0600(isolated_data_dir):
    secrets_store.set_secret(GITHUB, FAKE_TOKEN)

    assert stat.S_IMODE(env_file(isolated_data_dir).stat().st_mode) == 0o600


def test_rewrite_keeps_mode_0600_and_leaves_no_temp_files(isolated_data_dir):
    secrets_store.set_secret(GITHUB, FAKE_TOKEN)
    secrets_store.set_apprise_urls(["json://example.com"])
    secrets_store.clear_secret(GITHUB)

    assert stat.S_IMODE(env_file(isolated_data_dir).stat().st_mode) == 0o600
    assert [p.name for p in isolated_data_dir.iterdir()] == [".env"]


def test_write_goes_through_atomic_replace(isolated_data_dir, monkeypatch):
    replaced: list[str] = []
    real_replace = os.replace
    monkeypatch.setattr(
        os,
        "replace",
        lambda src, dst: (replaced.append(str(dst)), real_replace(src, dst)),
    )

    secrets_store.set_secret(GITHUB, FAKE_TOKEN)

    assert replaced == [str(env_file(isolated_data_dir))]


def test_failed_write_keeps_old_file(isolated_data_dir, monkeypatch):
    secrets_store.set_secret(GITHUB, FAKE_TOKEN)

    def boom(src, dst):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            secrets_store.set_secret(GITHUB, "fake-other-value-0003")

    assert secrets_store.get_secret(GITHUB) == FAKE_TOKEN
    assert [p.name for p in isolated_data_dir.iterdir()] == [".env"]


def test_other_lines_and_comments_are_preserved(isolated_data_dir):
    env_file(isolated_data_dir).write_text(
        "# my notes\nOTHER=keep me\n\nGITHUB_TOKEN=fake-old-value-0004\nlast=1\n"
    )

    secrets_store.set_secret(GITHUB, FAKE_TOKEN)

    assert env_file(isolated_data_dir).read_text() == (
        f"# my notes\nOTHER=keep me\n\nlast=1\nGITHUB_TOKEN={FAKE_TOKEN}\n"
    )


def test_unrecognised_names_are_never_loaded(isolated_data_dir):
    env_file(isolated_data_dir).write_text("OTHER=fake-value-0005\n")

    with pytest.raises(KeyError):
        secrets_store.get_secret("OTHER")
    assert set(secrets_store.get_status()) == {GITHUB}


def test_environment_beats_file(isolated_data_dir, monkeypatch):
    secrets_store.set_secret(GITHUB, FAKE_TOKEN)
    monkeypatch.setenv(GITHUB, "fake-env-value-0006")

    assert secrets_store.get_secret(GITHUB) == "fake-env-value-0006"
    assert secrets_store.secret_source(GITHUB) == "environment"
    with pytest.raises(secrets_store.SecretFromEnvironmentError):
        secrets_store.set_secret(GITHUB, "fake-other-value-0007")
    with pytest.raises(secrets_store.SecretFromEnvironmentError):
        secrets_store.clear_secret(GITHUB)
    assert FAKE_TOKEN in env_file(isolated_data_dir).read_text()


def test_set_never_touches_os_environ(isolated_data_dir):
    before = dict(os.environ)

    secrets_store.set_secret(GITHUB, FAKE_TOKEN)
    secrets_store.clear_secret(GITHUB)

    assert dict(os.environ) == before


@pytest.mark.parametrize(
    "bad",
    [
        "short",
        "x" * 513,
        "fake value with spaces",
        "fake-value-0008\nOTHER=injected",
        "fake-value-0008\n",
        "fake-value-0008\r",
        'fake"value-0009',
        "fake'value-0009",
        "fake`value-0009",
        "fake-valué-0010",
        "fake\tvalue-0011",
        "",
        None,
        12345678,
    ],
)
def test_invalid_values_are_rejected_without_echo(isolated_data_dir, bad):
    with pytest.raises(secrets_store.InvalidSecretError) as error:
        secrets_store.set_secret(GITHUB, bad)

    assert str(bad) not in str(error.value) or str(bad) == ""
    assert not env_file(isolated_data_dir).exists()


def test_length_bounds_are_accepted(isolated_data_dir):
    secrets_store.set_secret(GITHUB, "a" * 8)
    secrets_store.set_secret(GITHUB, "a" * 512)

    assert secrets_store.get_secret(GITHUB) == "a" * 512


def test_unknown_name_raises_key_error(isolated_data_dir):
    with pytest.raises(KeyError):
        secrets_store.set_secret("OTHER", FAKE_TOKEN)
    with pytest.raises(KeyError):
        secrets_store.clear_secret("OTHER")


def test_malformed_lines_do_not_raise(isolated_data_dir):
    env_file(isolated_data_dir).write_text(
        "GITHUB_TOKEN\n=\n\x00\xff garbage\nGITHUB_TOKEN=\n"
        "GITHUB_TOKEN=has space inside\n"
    )

    assert secrets_store.get_secret(GITHUB) is None
    assert secrets_store.get_status() == {GITHUB: None}
    secrets_store.set_secret(GITHUB, FAKE_TOKEN)
    assert secrets_store.get_secret(GITHUB) == FAKE_TOKEN


def test_redact_replaces_current_values(isolated_data_dir, monkeypatch):
    secrets_store.set_secret(GITHUB, FAKE_TOKEN)
    monkeypatch.setenv(GITHUB, FAKE_KEY)

    text = f"failed with {FAKE_TOKEN} and {FAKE_KEY} twice {FAKE_TOKEN}"

    assert secrets_store.redact(text) == "failed with *** and *** twice ***"
    assert secrets_store.redact("nothing here") == "nothing here"


def test_reads_data_dir_at_call_time(isolated_data_dir, tmp_path_factory, monkeypatch):
    other = tmp_path_factory.mktemp("other")
    monkeypatch.setattr(config_store, "DATA_DIR", other)

    secrets_store.set_secret(GITHUB, FAKE_TOKEN)

    assert (other / ".env").exists()
    assert not env_file(isolated_data_dir).exists()


EXCLUDED_CHARS = list("$;&|<>()*?[]{}\\#!~^%`\"',\\ ") + [
    "\x00", "\t", "\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x1f",
    "\x7f", "\x85", "\u2028", "\u2029", "\u00e9", "\u00a0",
]  # fmt: skip


@pytest.mark.parametrize("char", EXCLUDED_CHARS, ids=lambda c: f"U+{ord(c):04X}")
def test_every_excluded_character_is_rejected(isolated_data_dir, char):
    for value in (
        f"fake-{char}-value-1",
        f"fake-value-0020{char}",
        f"{char}fake-value-21",
    ):
        with pytest.raises(secrets_store.InvalidSecretError):
            secrets_store.set_secret(GITHUB, value)
    assert not env_file(isolated_data_dir).exists()


def test_every_allowed_character_is_accepted(isolated_data_dir):
    value = "aZ09._+/=:@-"

    secrets_store.set_secret(GITHUB, value)

    assert secrets_store.get_secret(GITHUB) == value


def test_temp_file_is_never_wider_than_0600(isolated_data_dir, monkeypatch):
    modes: list[int] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def spy_fsync(fd):
        modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        real_fsync(fd)

    def spy_replace(src, dst):
        modes.append(stat.S_IMODE(os.stat(src).st_mode))
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)

    secrets_store.set_secret(GITHUB, FAKE_TOKEN)

    assert modes and all(mode & 0o177 == 0 for mode in modes)


def test_existing_0644_file_ends_0600(isolated_data_dir):
    path = env_file(isolated_data_dir)
    path.write_text("OTHER=1\n")
    path.chmod(0o644)

    secrets_store.set_secret(GITHUB, FAKE_TOKEN)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_symlinked_env_is_replaced_not_written_through(
    isolated_data_dir, tmp_path_factory
):
    target = tmp_path_factory.mktemp("elsewhere") / "target"
    target.write_text("untouched\n")
    env_file(isolated_data_dir).symlink_to(target)

    secrets_store.set_secret(GITHUB, FAKE_TOKEN)

    assert not env_file(isolated_data_dir).is_symlink()
    assert target.read_text() == "untouched\n"
    assert secrets_store.get_secret(GITHUB) == FAKE_TOKEN


def test_export_prefix_and_duplicates(isolated_data_dir):
    env_file(isolated_data_dir).write_text(
        "export GITHUB_TOKEN=fake-first-value-1\nGITHUB_TOKEN=fake-second-value-2\n"
        "keep=me\n"
    )

    assert secrets_store.get_secret(GITHUB) == "fake-second-value-2"
    secrets_store.set_secret(GITHUB, FAKE_TOKEN)
    assert (
        env_file(isolated_data_dir).read_text()
        == f"keep=me\nGITHUB_TOKEN={FAKE_TOKEN}\n"
    )

    env_file(isolated_data_dir).write_text(
        "export GITHUB_TOKEN=fake-first-value-1\nGITHUB_TOKEN=fake-second-value-2\n"
    )
    secrets_store.clear_secret(GITHUB)
    assert secrets_store.get_secret(GITHUB) is None
    assert "GITHUB_TOKEN" not in env_file(isolated_data_dir).read_text()


@pytest.mark.parametrize("blank", ["", " ", "   \t "])
def test_blank_environment_value_does_not_count_as_set(
    isolated_data_dir, monkeypatch, blank
):
    monkeypatch.setenv(GITHUB, blank)

    assert secrets_store.get_secret(GITHUB) is None
    assert secrets_store.secret_source(GITHUB) is None
    secrets_store.set_secret(GITHUB, FAKE_TOKEN)
    assert secrets_store.secret_source(GITHUB) == "file"
    secrets_store.clear_secret(GITHUB)


def test_environment_value_is_stripped(isolated_data_dir, monkeypatch):
    monkeypatch.setenv(GITHUB, "  fake-env-value-0006\n")

    assert secrets_store.get_secret(GITHUB) == "fake-env-value-0006"


def test_concurrent_writers_keep_each_others_secrets(isolated_data_dir, monkeypatch):
    real_read = secrets_store._read_lines

    def slow_read():
        lines = real_read()
        time.sleep(0.002)
        return lines

    monkeypatch.setattr(secrets_store, "_read_lines", slow_read)
    errors: list[BaseException] = []

    def hammer_token() -> None:
        try:
            for index in range(15):
                secrets_store.set_secret(GITHUB, f"fake-gh-value-{index:04d}")
        except BaseException as error:  # noqa: BLE001 - surfaced by the assert
            errors.append(error)

    def hammer_urls() -> None:
        try:
            for index in range(15):
                secrets_store.set_apprise_urls([f"json://host-{index:04d}.example"])
        except BaseException as error:  # noqa: BLE001 - surfaced by the assert
            errors.append(error)

    threads = [
        threading.Thread(target=hammer_token),
        threading.Thread(target=hammer_urls),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert secrets_store.get_secret(GITHUB) == "fake-gh-value-0014"
    assert secrets_store.read_apprise_urls()[0] == ("json://host-0014.example",)


def test_non_ascii_preserved_line_survives_a_non_utf8_locale(isolated_data_dir):
    env_file(isolated_data_dir).write_text("# caf\u00e9\n", encoding="utf-8")
    script = (
        "import sys\nfrom pathlib import Path\nfrom src import config_store, secrets_store\n"
        "config_store.DATA_DIR = Path(sys.argv[1])\n"
        f"secrets_store.set_secret('GITHUB_TOKEN', '{FAKE_TOKEN}')\n"
    )
    environment = {
        "PATH": os.environ["PATH"],
        "LC_ALL": "C",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
    }

    result = subprocess.run(
        [sys.executable, "-c", script, str(isolated_data_dir)],
        cwd=Path(__file__).parent.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "caf\u00e9" in env_file(isolated_data_dir).read_text(encoding="utf-8")


def test_stale_temp_files_are_removed_at_the_next_write(isolated_data_dir):
    stale = isolated_data_dir / "..env-abcd1234"
    stale.write_text("GITHUB_TOKEN=fake-stale-value-1\n")
    other = isolated_data_dir / ".config-abcd1234"
    other.write_text("keep")

    secrets_store.set_secret(GITHUB, FAKE_TOKEN)

    assert not stale.exists()
    assert other.exists()


LEGACY_KEY = "CLOAKBROWSER_LICENSE_KEY"


def test_discard_legacy_license_key_removes_it_from_file_and_environment(
    isolated_data_dir, monkeypatch
):
    env_file(isolated_data_dir).write_text(f"{LEGACY_KEY}=fake-retired-key-0020\n")
    monkeypatch.setenv(LEGACY_KEY, "fake-retired-key-0020")

    secrets_store.discard_legacy_license_key()

    assert LEGACY_KEY not in os.environ
    assert LEGACY_KEY not in env_file(isolated_data_dir).read_text()


def test_discard_legacy_license_key_keeps_other_lines_and_mode(isolated_data_dir):
    path = env_file(isolated_data_dir)
    path.write_text(
        f"# notes\nOTHER=keep me\n{LEGACY_KEY}=fake-retired-key-0021\n"
        "GITHUB_TOKEN=fake-file-token-0022\n"
    )
    path.chmod(0o644)

    secrets_store.discard_legacy_license_key()

    assert path.read_text() == (
        "# notes\nOTHER=keep me\nGITHUB_TOKEN=fake-file-token-0022\n"
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_discard_legacy_license_key_leaves_the_file_alone_when_absent(
    isolated_data_dir,
):
    path = env_file(isolated_data_dir)
    path.write_text("OTHER=keep me\n")
    path.chmod(0o644)
    before = path.stat().st_mtime_ns

    secrets_store.discard_legacy_license_key()

    assert path.read_text() == "OTHER=keep me\n"
    assert path.stat().st_mtime_ns == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_discard_legacy_license_key_without_a_file_creates_nothing(
    isolated_data_dir,
):
    secrets_store.discard_legacy_license_key()

    assert not env_file(isolated_data_dir).exists()


def test_absorbing_the_environment_discards_the_legacy_license_key(
    isolated_data_dir, monkeypatch
):
    env_file(isolated_data_dir).write_text(f"{LEGACY_KEY}=fake-retired-key-0023\n")
    monkeypatch.setenv(LEGACY_KEY, "fake-retired-key-0023")

    secrets_store.absorb_environment()

    assert LEGACY_KEY not in os.environ
    assert LEGACY_KEY not in env_file(isolated_data_dir).read_text()
