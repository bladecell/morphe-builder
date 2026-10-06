"""No place that shows free text to an observer may carry a secret value."""

import logging
import sys
from typing import ClassVar

import pytest
import requests
from fastapi.testclient import TestClient
from requests.adapters import HTTPAdapter

from src import apkmirror_downloader, config_store, main, patcher, secrets_store

TOKEN = "fake-token-for-tests-0101"
KEY = "fake-licence-for-tests-0102"
BAD_ENV = "fake bad env value 0103"


@pytest.fixture(autouse=True)
def both_secrets(monkeypatch):
    """One secret comes from the environment, the other from the file."""
    monkeypatch.setenv(secrets_store.GITHUB_TOKEN, TOKEN)
    secrets_store._warned_invalid_env.clear()


@pytest.fixture
def stored_key(isolated_data_dir):
    secrets_store.set_secret(secrets_store.CLOAKBROWSER_LICENSE_KEY, KEY)


@pytest.fixture
def client(data_dir):
    return TestClient(main.app)


class InlineWorker:
    """Runs a task in the calling thread, as the scraper worker would."""

    def execute(self, func, *args, **kwargs):
        return func(*args, scraper=object(), **kwargs)


class FakeApprise:
    """Records every notification instead of sending it."""

    sent: ClassVar[list[dict[str, str]]] = []

    def add(self, url):
        return True

    def notify(self, body, title):
        FakeApprise.sent.append({"body": body, "title": title})
        return True


@pytest.fixture
def notifications(data_dir, monkeypatch):
    FakeApprise.sent = []
    monkeypatch.setattr(main.apprise, "Apprise", FakeApprise)

    def add_url(config: dict) -> None:
        config["settings"]["notification_urls"] = ["json://localhost"]

    config_store.update_config(add_url)
    return FakeApprise.sent


@pytest.fixture
def failing_build(monkeypatch, stored_key):
    """A build whose pipeline fails with text holding both secrets."""

    def explode(*args, **kwargs):
        print(f"pipeline output {TOKEN}")
        raise RuntimeError(f"boom {TOKEN} and {KEY}")

    monkeypatch.setattr(patcher, "run_pipeline", explode)
    monkeypatch.setattr(main, "SCRAPER_WORKER", InlineWorker())
    monkeypatch.setattr(
        main, "BUILD_STATUS", {"in_progress": False, "message": "Idle", "last_run": ""}
    )
    monkeypatch.setattr(main, "SCRAPER_ACTIVE", False)


def assert_clean(text: str) -> None:
    assert TOKEN not in text
    assert KEY not in text


# --- redact itself ---------------------------------------------------------------


def test_redact_masks_an_environment_value_that_is_not_a_valid_secret(monkeypatch):
    monkeypatch.setenv(secrets_store.CLOAKBROWSER_LICENSE_KEY, BAD_ENV)

    redacted = secrets_store.redact(f"quoted {BAD_ENV} here")

    assert BAD_ENV not in redacted
    assert redacted == "quoted *** here"


def test_redact_leaves_a_short_environment_value_alone(monkeypatch):
    monkeypatch.setenv(secrets_store.CLOAKBROWSER_LICENSE_KEY, "x")

    assert secrets_store.redact("xyz") == "xyz"


# --- build status, notification, build log, stdout --------------------------------


def test_failed_build_message_notification_and_logs_are_clean(
    failing_build, notifications, client, capsys
):
    main.build_task(package_ids=[], is_cron=False)

    assert main.BUILD_STATUS["message"].startswith("Failed: ")
    assert_clean(main.BUILD_STATUS["message"])
    assert_clean(client.get("/api/ui/build_status").text)
    assert [n["title"] for n in notifications] == ["Build Failed"]
    assert "boom" in notifications[0]["body"]
    assert_clean(notifications[0]["body"])
    logs = client.get("/api/ui/logs").json()["logs"]
    assert "pipeline output" in logs and "boom" in logs
    assert_clean(logs)
    assert_clean(main.LOG_PATH.read_text())
    assert_clean(capsys.readouterr().out)


def test_logs_route_redacts_a_file_written_before_the_secret_was_known(client):
    main.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    main.LOG_PATH.write_text(f"old line {TOKEN}\n")

    logs = client.get("/api/ui/logs").json()["logs"]

    assert logs == "old line ***\n"


def test_send_notification_redacts_title_and_body(notifications):
    main.send_notification(f"title {TOKEN}", f"body {TOKEN}")

    assert notifications == [{"title": "title ***", "body": "body ***"}]


def test_tee_redacts_what_it_forwards():
    class Sink:
        def __init__(self):
            self.text = ""

        def write(self, value):
            self.text += value

        def flush(self):
            pass

    first, second = Sink(), Sink()

    main.Tee(first, second).write(f"a {TOKEN} b")

    assert first.text == second.text == "a *** b"


# --- sync error and HTTP error details --------------------------------------------


def rate_limit_with(text: str) -> patcher.GitHubRateLimitError:
    error = patcher.GitHubRateLimitError()
    error.args = (text,)
    return error


def test_sync_error_on_the_discovery_tab_is_clean(client, monkeypatch):
    def limited(config, quiet=False, force=False):
        raise rate_limit_with(f"limit {TOKEN}")

    monkeypatch.setattr(main, "update_tools", limited)
    monkeypatch.setattr(main, "SYNC_ERROR", None)

    main.update_tools_task(config_store.load_config(), True)
    body = client.get("/api/ui/tools_status").json()

    assert body["sync_error"] == "limit ***"


def test_tools_status_redacts_an_error_stored_earlier(client, monkeypatch):
    monkeypatch.setattr(main, "SYNC_ERROR", f"stored {TOKEN}")

    assert client.get("/api/ui/tools_status").json()["sync_error"] == "stored ***"


def test_429_detail_is_clean(client, monkeypatch):
    def limited(repo, paths, force=False):
        raise rate_limit_with(f"limit {TOKEN}")

    monkeypatch.setattr(main, "check_and_download_release", limited)

    response = client.post("/api/ui/add_source", json={"repo": "own/new"})

    assert response.status_code == 429
    assert response.json() == {"detail": "limit ***"}


def test_502_detail_is_clean(client, data_dir, monkeypatch):
    json_path = patcher.get_source_paths("own/src")[".json"]
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text("[]")

    def unreadable(repo):
        raise patcher.PatchListError("own/src", f"cannot read {TOKEN}")

    monkeypatch.setattr(main, "load_patch_list", unreadable)

    response = client.get("/api/ui/patches/com.a")

    assert response.status_code == 502
    assert "(cannot read ***)" in response.json()["detail"]
    assert_clean(response.text)


def test_http_errors_without_a_secret_are_unchanged(client):
    response = client.get("/api/apps/nope")

    assert response.status_code == 404
    assert response.json() == {"detail": "App not found"}


def test_update_status_error_for_the_card_is_clean(client, monkeypatch):
    monkeypatch.setattr(main, "UPDATE_CACHE", {"timestamp": 0, "results": None})
    monkeypatch.setattr(main, "SCRAPER_ACTIVE", False)
    monkeypatch.setattr(main, "BUILD_STATUS", {"in_progress": False})
    monkeypatch.setattr(patcher, "get_patch_versions", lambda cfg: {})

    def unreadable(*args, **kwargs):
        raise patcher.PatchListError("own/src", f"unreadable {TOKEN}")

    monkeypatch.setattr(main, "get_supported_versions", unreadable)
    monkeypatch.setattr(main, "SCRAPER_WORKER", InlineWorker())

    body = client.get("/api/ui/updates").json()

    assert "(unreadable ***)" in body["com.a"]["error"]
    assert_clean(client.get("/api/ui/updates").text)


# --- the error text stored for an app card ----------------------------------------


def test_save_app_result_redacts_the_stored_error(data_dir):
    patcher.save_app_result("com.a", status="Error", error=f"bad {TOKEN}")

    saved = config_store.load_config()
    assert saved["apps"][0]["error"] == "bad ***"
    assert TOKEN not in (data_dir / "config.yaml").read_text()


def test_save_app_result_keeps_a_none_error(data_dir):
    patcher.save_app_result("com.a", status="Success", error=None)

    saved = config_store.load_config()
    assert saved["apps"][0]["error"] is None


def test_pipeline_failure_text_is_clean_in_config_and_output(
    pipeline, data_dir, monkeypatch, capsys
):
    def explode(*args, **kwargs):
        raise patcher.PatchSelectionError(f"nothing applied {TOKEN}")

    monkeypatch.setattr(patcher, "patch_app", explode)
    monkeypatch.setattr(patcher.time, "sleep", lambda seconds: None)

    patcher.run_pipeline(["com.a"], quiet=True, scraper=object())

    assert "nothing applied" in capsys.readouterr().out
    saved = config_store.load_config()
    assert saved["apps"][0]["error"] == "nothing applied ***"


def test_unexpected_pipeline_exception_is_clean(
    pipeline, data_dir, monkeypatch, capsys
):
    def explode(*args, **kwargs):
        raise OSError(f"disk {TOKEN}")

    monkeypatch.setattr(patcher, "patch_app", explode)

    patcher.run_pipeline(["com.a"], quiet=True, scraper=object())

    out = capsys.readouterr().out
    assert "Error processing com.a" in out
    assert_clean(out)
    assert_clean((data_dir / "config.yaml").read_text())


# --- prints ------------------------------------------------------------------------


def test_patcher_print_redacts_every_value_and_keeps_print_semantics(capsys):
    patcher.print("a", TOKEN, "b", sep="-", end="!")

    assert capsys.readouterr().out == "a-***-b!"


def test_patcher_print_still_writes_to_a_given_file(capsys):
    patcher.print(f"x {TOKEN}", file=sys.stderr)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "x ***\n"


def test_failed_release_fetch_print_is_clean(data_dir, monkeypatch, capsys):
    def refuse(adapter, request, **kwargs):
        # The token is set, so the request goes through the authenticated session.
        raise requests.exceptions.ConnectionError(f"refused {TOKEN}")

    monkeypatch.setattr(HTTPAdapter, "send", refuse)

    result = patcher.check_and_download_release(
        "own/src", {".jar": data_dir / "x.jar"}, force=True
    )

    assert result is False
    out = capsys.readouterr().out
    assert "Failed to fetch updates" in out
    assert_clean(out)


def test_rate_limit_print_in_run_pipeline_is_clean(monkeypatch, capsys):
    monkeypatch.setattr(patcher, "load_config", lambda: {"apps": []})

    def limited(config, quiet=False, force=False):
        raise rate_limit_with(f"limit {TOKEN}")

    monkeypatch.setattr(patcher, "update_tools", limited)
    monkeypatch.setattr(patcher, "get_patch_versions", lambda cfg: {})

    patcher.run_pipeline(quiet=True, scraper=object())

    out = capsys.readouterr().out
    assert "limit ***" in out


def test_cron_failure_is_logged_without_the_value(monkeypatch, caplog, capsys):
    def refuse(*args, **kwargs):
        raise ValueError(f"bad schedule {TOKEN}")

    monkeypatch.setattr(main.scheduler, "add_job", refuse)

    with caplog.at_level(logging.ERROR, logger=main.logger.name):
        main.setup_cron_job({"settings": {"cron_schedule": "0 * * * *"}})

    assert "bad schedule" in caplog.text
    assert_clean(caplog.text)
    assert_clean(capsys.readouterr().out)


# --- logging -----------------------------------------------------------------------


def test_logging_redacts_message_arguments(caplog, stored_key):
    logger = logging.getLogger("src.some_module")

    with caplog.at_level(logging.INFO, logger="src.some_module"):
        logger.error("failed with %s", TOKEN)
        logger.info("direct " + KEY)

    assert "failed with ***" in caplog.text
    assert_clean(caplog.text)


def test_logging_redacts_a_traceback(caplog, stored_key):
    logger = logging.getLogger("src.some_module")

    with caplog.at_level(logging.ERROR, logger="src.some_module"):
        try:
            raise RuntimeError(f"inner {TOKEN} {KEY}")
        except RuntimeError:
            logger.exception("it broke")

    assert "RuntimeError: inner" in caplog.text
    assert_clean(caplog.text)


def test_logging_survives_a_value_that_is_invalid_in_the_file(
    isolated_data_dir, monkeypatch, caplog
):
    """Reading the secrets logs a warning itself; that must not recurse."""
    monkeypatch.delenv(secrets_store.GITHUB_TOKEN)
    (isolated_data_dir / ".env").write_text("GITHUB_TOKEN=bad value\n")
    logger = logging.getLogger("src.some_module")

    with caplog.at_level(logging.WARNING):
        logger.warning("hello")

    assert "hello" in caplog.text


def test_scraper_logger_output_is_clean(capsys):
    apkmirror_downloader.logger.error("navigation failed: %s", TOKEN)

    out = capsys.readouterr().out
    assert "navigation failed: ***" in out
    assert_clean(out)
