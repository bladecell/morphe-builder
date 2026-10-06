"""Second hardening round: log records, remembered values, startup removal."""

import base64
import io
import logging
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
import requests
from fastapi.testclient import TestClient
from requests.adapters import HTTPAdapter
from uvicorn.logging import AccessFormatter, DefaultFormatter

from src import config_store, main, patcher, secrets_store

GITHUB = secrets_store.GITHUB_TOKEN
LICENCE = secrets_store.CLOAKBROWSER_LICENSE_KEY
TOKEN = "fake-token-for-tests-0301"
OLD_TOKEN = "fake-old-token-for-tests-0302"
KEY = "fake-licence-for-tests-0303"
FILE_TOKEN = "fake-file-token-for-tests-0304"


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """Every test starts with nothing captured, remembered or warned about."""
    monkeypatch.setattr(secrets_store, "_captured_environment", {}, raising=False)
    monkeypatch.setattr(secrets_store, "_known", (), raising=False)
    monkeypatch.setattr(secrets_store, "_warned_invalid_env", set())
    monkeypatch.setattr(secrets_store, "_warned_invalid_file", set(), raising=False)


def make_record(msg, args=(), exc_info=None, name="src.some_module"):
    """Create a record the way `Logger._log` does, through the active factory."""
    factory = logging.getLogRecordFactory()
    return factory(name, logging.ERROR, "f.py", 1, msg, args, exc_info)


# --- F1: the factory never raises and never leaves a secret behind -------------


class UnprintableArgument:
    """Its __str__ fails and its __repr__ shows the secret."""

    def __str__(self):
        raise RuntimeError("no str")

    def __repr__(self):
        return f"Unprintable({TOKEN})"


class ReprOnlyArgument:
    """Its __str__ is clean and its __repr__ shows the secret."""

    def __str__(self):
        return "clean"

    def __repr__(self):
        return f"ReprOnly({TOKEN})"


def test_missing_key_in_a_dict_argument_does_not_raise_or_leak(monkeypatch):
    monkeypatch.setenv(GITHUB, TOKEN)

    record = make_record("%(a)s", ({"b": TOKEN},))

    assert TOKEN not in repr(vars(record))


def test_an_argument_whose_str_raises_does_not_raise_or_leak(monkeypatch):
    monkeypatch.setenv(GITHUB, TOKEN)

    record = make_record("value %s", (UnprintableArgument(),))

    assert TOKEN not in repr(vars(record))
    assert "value" in record.getMessage()


def test_an_argument_whose_repr_shows_the_secret_is_masked(monkeypatch):
    monkeypatch.setenv(GITHUB, TOKEN)

    record = make_record("value %r", (ReprOnlyArgument(),))

    assert TOKEN not in record.getMessage()


def test_a_bad_format_keeps_redacted_arguments(monkeypatch):
    monkeypatch.setenv(GITHUB, TOKEN)

    record = make_record("needs two %s %s", (TOKEN,))

    assert TOKEN not in repr(vars(record))
    assert record.args == ("***",)


def test_a_bad_format_is_not_printed_unredacted_by_handle_error(monkeypatch, capsys):
    monkeypatch.setenv(GITHUB, TOKEN)
    logger = logging.getLogger("src.bad_format")
    logger.propagate = False  # pytest's own handlers re-raise formatting errors
    handler = logging.StreamHandler(io.StringIO())
    logger.addHandler(handler)
    try:
        logger.error("needs two %s %s", TOKEN)  # noqa: PLE1206 - the bug under test
    finally:
        logger.removeHandler(handler)

    captured = capsys.readouterr()
    assert TOKEN not in captured.err and TOKEN not in captured.out


def test_a_factory_failure_fails_closed(monkeypatch):
    monkeypatch.setenv(GITHUB, TOKEN)

    def broken(text):
        raise RuntimeError("redact is broken")

    monkeypatch.setattr(secrets_store, "redact", broken)

    record = make_record(f"leaks {TOKEN}", (TOKEN,))

    assert TOKEN not in repr(vars(record))


def test_exc_info_is_rendered_redacted_and_dropped(monkeypatch):
    monkeypatch.setenv(GITHUB, TOKEN)
    try:
        raise RuntimeError(f"inner {TOKEN}")
    except RuntimeError:
        info = sys.exc_info()

    record = make_record("it broke", exc_info=info)

    assert record.exc_info is None
    assert "RuntimeError: inner ***" in record.exc_text
    assert TOKEN not in repr(vars(record))


def test_a_recursive_redact_does_not_recurse_forever(monkeypatch):
    """A record logged while redacting must be left alone (the guard)."""
    monkeypatch.setenv(GITHUB, TOKEN)
    real_redact = secrets_store.redact
    depth = []

    def logging_redact(text):
        depth.append(1)
        logging.getLogger("src.inner").warning("inside redact")
        return real_redact(text)

    monkeypatch.setattr(secrets_store, "redact", logging_redact)

    make_record("hello")

    assert len(depth) < 20


def test_uvicorn_access_formatter_still_renders_the_arguments(monkeypatch):
    monkeypatch.setenv(GITHUB, TOKEN)
    record = make_record(
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", f"/x?t={TOKEN}", "1.1", 200),
        name="uvicorn.access",
    )
    formatter = AccessFormatter(
        '%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False
    )

    line = formatter.format(record)

    assert line == '127.0.0.1:5000 - "GET /x?t=*** HTTP/1.1" 200 OK'


def test_uvicorn_default_formatter_still_renders_the_arguments(monkeypatch):
    monkeypatch.setenv(GITHUB, TOKEN)
    record = make_record(
        "Started %s at %d", (f"server {TOKEN}", 8000), name="uvicorn.error"
    )
    formatter = DefaultFormatter("%(levelprefix)s %(message)s", use_colors=True)

    line = formatter.format(record)

    assert "Started server *** at 8000" in line
    assert "%s" not in line


def test_non_secret_arguments_keep_their_type(monkeypatch):
    monkeypatch.setenv(GITHUB, TOKEN)

    record = make_record("%d %s", (5, "plain"))

    assert record.args == (5, "plain")


# --- F2: values stay masked for the life of the process ------------------------


def test_a_rotated_environment_value_stays_masked(monkeypatch):
    monkeypatch.setenv(GITHUB, OLD_TOKEN)
    assert secrets_store.redact(OLD_TOKEN) == "***"

    monkeypatch.setenv(GITHUB, TOKEN)

    assert secrets_store.redact(f"old {OLD_TOKEN} new {TOKEN}") == "old *** new ***"


def test_a_cleared_environment_value_stays_masked(monkeypatch):
    monkeypatch.setenv(GITHUB, TOKEN)
    secrets_store.get_secret(GITHUB)

    monkeypatch.delenv(GITHUB)

    assert secrets_store.redact(f"x {TOKEN}") == "x ***"


def test_a_cleared_file_value_stays_masked(isolated_data_dir):
    secrets_store.set_secret(GITHUB, FILE_TOKEN)
    secrets_store.get_secret(GITHUB)

    secrets_store.clear_secret(GITHUB)

    assert secrets_store.get_secret(GITHUB) is None
    assert secrets_store.redact(f"x {FILE_TOKEN}") == "x ***"


def test_a_value_just_stored_is_masked_before_it_is_ever_read(isolated_data_dir):
    secrets_store.set_secret(GITHUB, FILE_TOKEN)

    assert secrets_store.redact(f"x {FILE_TOKEN}") == "x ***"


def test_an_unreadable_file_does_not_unmask_a_known_value(
    isolated_data_dir, monkeypatch
):
    secrets_store.set_secret(GITHUB, FILE_TOKEN)
    secrets_store.get_secret(GITHUB)

    def unreadable():
        raise OSError("disk gone")

    monkeypatch.setattr(secrets_store, "_read_lines", unreadable)

    assert secrets_store.redact(f"x {FILE_TOKEN}") == "x ***"


def test_redact_does_not_read_the_file(isolated_data_dir, monkeypatch):
    secrets_store.set_secret(GITHUB, FILE_TOKEN)
    reads: list[int] = []
    real_read = secrets_store._read_lines
    monkeypatch.setattr(
        secrets_store, "_read_lines", lambda: reads.append(1) or real_read()
    )

    for _ in range(50):
        secrets_store.redact(f"line {FILE_TOKEN}")

    assert reads == []


def test_a_file_value_is_masked_when_the_environment_overrides_it(
    isolated_data_dir, monkeypatch
):
    secrets_store.set_secret(GITHUB, FILE_TOKEN)
    monkeypatch.setenv(GITHUB, TOKEN)
    secrets_store.absorb_environment()

    assert secrets_store.redact(f"{FILE_TOKEN} {TOKEN}") == "*** ***"


def test_a_valid_environment_value_still_wins_over_a_file_value(
    isolated_data_dir, monkeypatch
):
    secrets_store.set_secret(GITHUB, FILE_TOKEN)
    monkeypatch.setenv(GITHUB, TOKEN)

    assert secrets_store.get_secret(GITHUB) == TOKEN
    assert secrets_store.secret_source(GITHUB) == "environment"


def test_a_header_dict_repr_is_masked(monkeypatch):
    monkeypatch.setenv(GITHUB, TOKEN)
    headers = {"Authorization": f"token {TOKEN}", "Accept": "json"}

    assert TOKEN not in secrets_store.redact(f"request failed: {headers!r}")


def test_an_invalid_header_message_quoting_the_value_is_masked(monkeypatch):
    monkeypatch.setenv(GITHUB, TOKEN)
    value = f"token {TOKEN}\n"
    message = (
        "Invalid leading whitespace, reserved character(s), or return "
        f"character(s) in header value: {value!r}"
    )

    assert TOKEN not in secrets_store.redact(message)


@pytest.mark.xfail(
    strict=True,
    reason="Known limit: redact matches exact values only, so a Basic "
    "(base64) form of the secret is not masked",
)
def test_a_basic_auth_base64_form_is_masked(monkeypatch):
    monkeypatch.setenv(GITHUB, TOKEN)
    encoded = base64.b64encode(f"user:{TOKEN}".encode()).decode()

    assert encoded not in secrets_store.redact(f"Authorization: Basic {encoded}")


# --- D1: the secrets leave the process environment at startup ------------------


def test_absorbing_removes_both_variables_and_keeps_them_available(monkeypatch):
    monkeypatch.setenv(GITHUB, TOKEN)
    monkeypatch.setenv(LICENCE, KEY)

    secrets_store.absorb_environment()

    assert GITHUB not in os.environ and LICENCE not in os.environ
    assert secrets_store.get_secret(GITHUB) == TOKEN
    assert secrets_store.get_secret(LICENCE) == KEY
    assert secrets_store.get_status() == {GITHUB: "environment", LICENCE: "environment"}


def test_absorbed_values_are_still_locked_and_masked(monkeypatch, isolated_data_dir):
    monkeypatch.setenv(GITHUB, TOKEN)

    secrets_store.absorb_environment()

    with pytest.raises(secrets_store.SecretFromEnvironmentError):
        secrets_store.set_secret(GITHUB, FILE_TOKEN)
    assert secrets_store.redact(f"x {TOKEN}") == "x ***"


def test_an_invalid_value_is_removed_too_and_still_reported_invalid(monkeypatch):
    monkeypatch.setenv(GITHUB, "bad value with spaces")

    secrets_store.absorb_environment()

    assert GITHUB not in os.environ
    assert secrets_store.get_secret(GITHUB) is None
    assert secrets_store.secret_source(GITHUB) == "environment_invalid"


def test_a_blank_variable_is_removed_and_counts_as_unset(monkeypatch):
    monkeypatch.setenv(GITHUB, "   ")

    secrets_store.absorb_environment()

    assert GITHUB not in os.environ
    assert secrets_store.secret_source(GITHUB) is None


def test_absorbing_twice_keeps_the_values(monkeypatch):
    monkeypatch.setenv(GITHUB, TOKEN)
    secrets_store.absorb_environment()

    secrets_store.absorb_environment()

    assert secrets_store.get_secret(GITHUB) == TOKEN


def test_nothing_started_after_absorbing_inherits_the_secrets(monkeypatch):
    monkeypatch.setenv(GITHUB, TOKEN)
    monkeypatch.setenv(LICENCE, KEY)
    secrets_store.absorb_environment()

    result = subprocess.run(  # a plain subprocess call: it inherits os.environ
        [sys.executable, "-c", "import os; print(sorted(os.environ))"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert GITHUB not in result.stdout and LICENCE not in result.stdout


def test_app_startup_absorbs_before_anything_is_started(data_dir, monkeypatch):
    monkeypatch.setenv(GITHUB, TOKEN)
    monkeypatch.setenv(LICENCE, KEY)
    seen: list[bool] = []

    class FakeWorker:
        def start(self):
            seen.append(GITHUB in os.environ or LICENCE in os.environ)

        def shutdown(self):
            pass

        def is_alive(self):
            return False

    class FakeScheduler:
        running = False

        def start(self):
            seen.append(GITHUB in os.environ)

        def add_job(self, *args, **kwargs):
            pass

        def shutdown(self, wait=True):
            pass

    def deps(config):
        seen.append(GITHUB in os.environ or LICENCE in os.environ)
        return {}

    monkeypatch.setattr(main, "SCRAPER_WORKER", FakeWorker())
    monkeypatch.setattr(main, "scheduler", FakeScheduler())
    monkeypatch.setattr(main, "update_tools_task", lambda *a, **k: None)
    monkeypatch.setattr(main, "setup_cron_job", lambda config: None)
    monkeypatch.setattr(main, "check_dependencies", deps)

    with TestClient(main.app):
        assert secrets_store.get_secret(GITHUB) == TOKEN
        assert secrets_store.get_secret(LICENCE) == KEY

    assert seen == [False, False, False]
    assert GITHUB not in os.environ and LICENCE not in os.environ


# --- also fix: an invalid file entry warns once ---------------------------------


def test_an_invalid_file_entry_warns_once_by_name(isolated_data_dir, caplog):
    (isolated_data_dir / ".env").write_text("GITHUB_TOKEN=bad value here\n")
    caplog.set_level(logging.WARNING, logger="src.secrets_store")

    for _ in range(3):
        secrets_store.get_secret(GITHUB)
        secrets_store.get_status()

    warnings = [r for r in caplog.records if r.name == "src.secrets_store"]
    assert len(warnings) == 1
    assert GITHUB in warnings[0].getMessage()
    assert "bad value here" not in caplog.text


def test_the_file_warning_comes_back_after_the_entry_was_fixed(
    isolated_data_dir, caplog
):
    env_file = isolated_data_dir / ".env"
    caplog.set_level(logging.WARNING, logger="src.secrets_store")
    env_file.write_text("GITHUB_TOKEN=bad value here\n")
    secrets_store.get_secret(GITHUB)
    env_file.write_text(f"GITHUB_TOKEN={FILE_TOKEN}\n")
    secrets_store.get_secret(GITHUB)
    env_file.write_text("GITHUB_TOKEN=bad value again\n")
    secrets_store.get_secret(GITHUB)

    assert len([r for r in caplog.records if GITHUB in r.getMessage()]) == 2


# --- also fix: 409 tells an invalid environment value apart ---------------------


@pytest.fixture
def client(data_dir):
    return TestClient(main.app)


def test_409_for_a_valid_environment_value_keeps_its_message(client, monkeypatch):
    monkeypatch.setenv(GITHUB, TOKEN)

    response = client.put("/api/ui/secrets/github-token", json={"value": FILE_TOKEN})

    assert response.status_code == 409
    assert response.json() == {"detail": "Secret is set by the environment"}


def test_409_for_an_invalid_environment_value_says_so(client, monkeypatch):
    monkeypatch.setenv(GITHUB, "bad value with spaces")

    put = client.put("/api/ui/secrets/github-token", json={"value": FILE_TOKEN})
    delete = client.delete("/api/ui/secrets/github-token")

    expected = {"detail": "Secret is set by the environment but its value is invalid"}
    assert put.status_code == 409 and put.json() == expected
    assert delete.status_code == 409 and delete.json() == expected


# --- also fix: java output is redacted outside a web build ----------------------


def test_cli_output_is_redacted_without_the_build_tee(data_dir, monkeypatch, capsys):
    monkeypatch.setenv(GITHUB, TOKEN)
    paths = patcher.get_source_paths(patcher.BASE_PATCHES_REPO)
    paths[".mpp"].parent.mkdir(parents=True, exist_ok=True)
    paths[".mpp"].write_bytes(b"")
    paths[".json"].write_text('{"patches": []}')

    class FakePopen:
        def __init__(self, command, **kwargs):
            self.lines = iter([f"INFO: leaked {TOKEN}\n"])
            self.stdout = SimpleNamespace(readline=lambda: next(self.lines, ""))
            self.returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(patcher.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        patcher.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="")
    )
    app = {"id": "com.x", "name": "X", "include_patches": [], "exclude_patches": []}

    patcher.patch_app(app, {"cli_jar": "bin/cli.jar"}, "in.apk", "out.apk", {})

    out = capsys.readouterr().out
    assert "leaked ***" in out
    assert TOKEN not in out


# --- D3 and the non-string repo: invalid sources are reported -------------------


@pytest.fixture
def requests_made(monkeypatch):
    calls: list[str] = []

    def record(url, *args, **kwargs):
        calls.append(url)
        raise requests.exceptions.ConnectionError("blocked")

    def refuse_send(adapter, request, **kwargs):
        # The authenticated GitHub path builds its own session.
        return record(request.url)

    monkeypatch.setattr(patcher.requests, "get", record)
    monkeypatch.setattr(HTTPAdapter, "send", refuse_send)
    return calls


def add_sources(*sources: dict) -> None:
    def apply(config: dict) -> None:
        config["sources"].extend(sources)

    config_store.update_config(apply)


def sync_config() -> dict:
    config = config_store.load_config()
    config["repositories"] = {"cli": "x/cli", "apkeditor": "x/edit"}
    return config


@pytest.mark.parametrize("bad", [123, None, ["a", "b"], {"a": 1}, "../..", ""])
def test_a_source_with_a_bad_repo_is_skipped_without_stopping_the_sync(
    data_dir, requests_made, bad
):
    add_sources({"name": "bad", "repo": bad, "active": True})

    patcher.update_tools(sync_config(), quiet=True, force=True)

    assert any("/repos/own/src/" in url for url in requests_made)
    assert not any("/repos/../" in url for url in requests_made)


def test_a_source_without_a_repo_key_is_skipped(data_dir, requests_made):
    add_sources({"name": "bad", "active": True})

    patcher.update_tools(sync_config(), quiet=True, force=True)

    assert any("/repos/own/src/" in url for url in requests_made)


def test_the_discovery_sync_error_names_an_invalid_source(
    data_dir, requests_made, client, monkeypatch
):
    add_sources({"name": "Broken Source", "repo": "../..", "active": True})
    monkeypatch.setattr(main, "SYNC_ERROR", None)

    main.update_tools_task(config_store.load_config(), True)
    body = client.get("/api/ui/tools_status").json()

    assert "Broken Source" in body["sync_error"]
    assert "own/src" not in body["sync_error"]


def test_the_sync_error_names_a_source_whose_repo_is_not_text(
    data_dir, requests_made, client, monkeypatch
):
    add_sources({"name": "Numeric", "repo": 123, "active": True})
    monkeypatch.setattr(main, "SYNC_ERROR", None)

    main.update_tools_task(config_store.load_config(), True)

    assert "Numeric" in client.get("/api/ui/tools_status").json()["sync_error"]


def test_no_sync_error_when_every_source_is_valid(
    data_dir, requests_made, client, monkeypatch
):
    monkeypatch.setattr(main, "SYNC_ERROR", "stale")

    main.update_tools_task(config_store.load_config(), True)

    assert client.get("/api/ui/tools_status").json()["sync_error"] is None


def test_an_inactive_invalid_source_is_not_reported(
    data_dir, requests_made, client, monkeypatch
):
    add_sources({"name": "Off", "repo": "../..", "active": False})
    monkeypatch.setattr(main, "SYNC_ERROR", None)

    main.update_tools_task(config_store.load_config(), True)

    assert client.get("/api/ui/tools_status").json()["sync_error"] is None


def test_the_invalid_source_error_is_redacted(
    data_dir, requests_made, client, monkeypatch
):
    monkeypatch.setenv(GITHUB, TOKEN)
    add_sources({"name": f"Name {TOKEN}", "repo": "../..", "active": True})
    monkeypatch.setattr(main, "SYNC_ERROR", None)

    main.update_tools_task(config_store.load_config(), True)

    assert TOKEN not in client.get("/api/ui/tools_status").json()["sync_error"]


# --- D4: two repos may not share local files ------------------------------------

BASE = "MorpheApp/morphe-patches"
TWIN = "morpheapp-morphe/patches"


def test_both_names_map_to_the_same_files():
    """The premise of the collision: two valid repos, one set of files."""
    assert patcher.is_valid_repo(BASE) and patcher.is_valid_repo(TWIN)
    assert patcher.get_source_paths(BASE) == patcher.get_source_paths(TWIN)


def test_adding_a_source_that_collides_is_refused_and_writes_nothing(
    data_dir, requests_made, client
):
    add_sources({"name": "Base", "repo": BASE, "active": True})
    before = config_store.load_config()

    response = client.post("/api/ui/add_source", json={"repo": TWIN})

    assert response.status_code == 400
    assert BASE in response.json()["detail"]
    assert requests_made == []
    assert config_store.load_config() == before
    assert not patcher.get_source_paths(TWIN)[".json"].exists()


def test_a_collision_with_an_inactive_source_is_refused_too(
    data_dir, requests_made, client
):
    add_sources({"name": "Base", "repo": BASE, "active": False})

    response = client.post("/api/ui/add_source", json={"repo": TWIN})

    assert response.status_code == 400
    assert requests_made == []


def test_a_hand_edited_pair_syncs_only_the_first_and_reports_the_second(
    data_dir, requests_made, client, monkeypatch
):
    add_sources(
        {"name": "First", "repo": BASE, "active": True},
        {"name": "Second", "repo": TWIN, "active": True},
    )
    monkeypatch.setattr(main, "SYNC_ERROR", None)

    main.update_tools_task(config_store.load_config(), True)

    assert any(f"/repos/{BASE}/" in url for url in requests_made)
    assert not any(f"/repos/{TWIN}/" in url for url in requests_made)
    error = client.get("/api/ui/tools_status").json()["sync_error"]
    assert "Second" in error and "First" in error
