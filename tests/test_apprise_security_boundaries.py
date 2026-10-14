"""Security-boundary evidence for Apprise redaction and startup ordering."""

import json
import logging
import threading

import pytest
from fastapi.testclient import TestClient

from src import config_store, main, secrets_store


def test_real_apprise_worker_keeps_rotated_credentials_masked_until_notify_finishes(
    data_dir, caplog, monkeypatch
) -> None:
    import requests

    entered = threading.Event()
    release = threading.Event()
    credential = "fake-discord-worker-token-987654321"
    webhook_id = "987654321012345678"
    url = f"discord://{webhook_id}/{credential}"
    caplog.set_level(logging.DEBUG)

    class Response:
        status_code = 204
        content = b""

        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

    def blocked_transport(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        logging.getLogger("test-discord-transport").warning("credential=%s", credential)
        return Response()

    monkeypatch.setattr(requests, "post", blocked_transport)
    result: list[bool] = []
    worker = threading.Thread(
        target=lambda: result.append(main._send_apprise((url,), "title", "body"))
    )
    worker.start()
    try:
        assert entered.wait(timeout=5)
        for index in range(secrets_store.MAX_RETIRED_APPRISE_MASK_SETS + 4):
            secrets_store.remember_apprise_masks((f"rotated-fake-token-{index:03d}",))
        assert secrets_store.redact(credential) == "***"
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert result == [True]
    assert credential not in caplog.text


def test_real_apprise_exception_message_with_private_credential_is_masked_and_released(
    data_dir, caplog, monkeypatch
) -> None:
    import requests

    credential = "fake-discord-exception-token-123456789"
    url = f"discord://987654321012345678/{credential}"
    caplog.set_level(logging.DEBUG)

    def failed_transport(*args, **kwargs):
        raise requests.ConnectionError(f"transport rejected {credential}")

    monkeypatch.setattr(requests, "post", failed_transport)
    assert main._send_apprise((url,), "title", "body") is False

    assert credential not in caplog.text
    assert not secrets_store._active_apprise_masks


def test_apprise_alias_private_fields_are_masked_without_registering_query_values(
    data_dir,
) -> None:
    # Apprise's kavenegar schema maps its private `apikey` token to `api_key`.
    api_key = "fake-kavenegar-alias-key-123456"
    url = f"kavenegar://{api_key}/09123456789?topic=ordinary-fake-topic"

    masks = main._apprise_redaction_values((url,))
    secrets_store.remember_apprise_masks(masks)

    assert secrets_store.redact(api_key) == "***"
    assert "ordinary-fake-topic" not in masks
    assert secrets_store.redact("ordinary-fake-topic") == "ordinary-fake-topic"


@pytest.mark.parametrize("source", ["environment", "file", "legacy"])
def test_startup_registers_preexisting_secrets_before_status_and_logs_requests(
    data_dir, monkeypatch, source: str
) -> None:
    credential = "fake-startup-discord-token-123456789"
    url = f"discord://987654321012345678/{credential}"
    if source == "environment":
        monkeypatch.setenv("APPRISE_URLS", json.dumps([url]))
    elif source == "file":
        secrets_store.set_apprise_urls([url])
    else:
        config_store.update_config(
            lambda config: config["settings"].update(notification_urls=[url])
        )
    # Startup operations unrelated to registration are isolated; no browser,
    # scheduler job, or tool sync is launched by this test.
    monkeypatch.setattr(main, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(main, "remove_legacy_cloakbrowser_cache", lambda: None)
    monkeypatch.setattr(main, "setup_cron_job", lambda config: None)
    monkeypatch.setattr(main, "check_dependencies", lambda config: {})
    monkeypatch.setattr(main.SCRAPER_WORKER, "start", lambda: None)
    monkeypatch.setattr(main.scheduler, "start", lambda: None)
    monkeypatch.setattr(main.scheduler, "add_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "update_tools_task", lambda config: None)

    main.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    main.LOG_PATH.write_text(f"existing diagnostic {credential}\n")
    import asyncio

    asyncio.run(main.startup_event())
    client = TestClient(main.app)
    status = client.get("/api/ui/secrets")
    logs = client.get("/api/ui/logs").json()["logs"]

    assert status.status_code == 200
    assert credential not in logs
    assert secrets_store.redact(credential) == "***"
