"""Write-only storage and explicit migration for Apprise notification URLs."""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from src import config_store, main, secrets_store


def test_legacy_urls_are_reported_without_values_or_rendered_into_page(
    data_dir,
) -> None:
    legacy_url = "json://fake-user:fake-secret@localhost"
    config_store.update_config(
        lambda config: config["settings"].update(notification_urls=[legacy_url])
    )
    client = TestClient(main.app)

    status = client.get("/api/ui/secrets").json()["apprise-urls"]
    page = client.get("/").text

    assert status == {
        "set": True,
        "count": 1,
        "source": "legacy",
        "legacy_available": True,
        "migration_state": "pending",
    }
    assert legacy_url not in json.dumps(status)
    assert legacy_url not in page


def test_general_settings_rejects_nonempty_notification_url_without_echo(
    data_dir,
) -> None:
    legacy_url = "json://fake-user:legacy-secret@localhost"
    submitted_url = "json://fake-user:submitted-secret@localhost"
    config_store.update_config(
        lambda config: config["settings"].update(notification_urls=[legacy_url])
    )

    response = TestClient(main.app).post(
        "/api/ui/save_settings",
        json={
            "server_url": "http://x",
            "cron_schedule": "0 * * * *",
            "notification_urls": [submitted_url],
        },
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Notification URLs must be managed in Secrets"}
    assert submitted_url not in response.text
    assert config_store.load_config()["settings"]["notification_urls"] == [legacy_url]


def test_put_and_delete_use_write_only_file_and_suppress_legacy_fallback(
    data_dir,
) -> None:
    legacy_url = "json://legacy-user:legacy-secret@localhost"
    url = "json://new-user:new-secret@localhost"
    config_store.update_config(
        lambda config: config["settings"].update(notification_urls=[legacy_url])
    )
    client = TestClient(main.app)

    response = client.put("/api/ui/secrets/apprise-urls", json={"value": [url]})
    assert response.status_code == 200
    assert response.json()["apprise-urls"]["source"] == "file"
    assert url not in response.text and legacy_url not in response.text
    assert config_store.load_config()["settings"].get("notification_urls") is None
    env_text = (data_dir / ".env").read_text()
    assert "APPRISE_URLS=" in env_text
    assert json.loads(env_text.removeprefix("APPRISE_URLS=").strip()) == [url]
    assert os.stat(data_dir / ".env").st_mode & 0o777 == 0o600

    deleted = client.delete("/api/ui/secrets/apprise-urls")
    assert deleted.status_code == 200
    assert main._effective_apprise_urls(config_store.load_config()) == ()
    assert config_store.load_config()["settings"].get("notification_urls") is None
    assert secrets_apprise_value(data_dir) == "APPRISE_URLS=[]"


def secrets_apprise_value(data_dir) -> str:
    return next(
        line
        for line in (data_dir / ".env").read_text().splitlines()
        if line.startswith("APPRISE_URLS=")
    )


def test_explicit_migration_is_idempotent_and_never_returns_url(data_dir) -> None:
    url = "json://migration-user:migration-secret@localhost"
    config_store.update_config(
        lambda config: config["settings"].update(notification_urls=[url])
    )
    client = TestClient(main.app)

    migrated = client.post(main.APPRISE_MIGRATION_PATH)
    assert migrated.status_code == 200
    assert migrated.json()["apprise-urls"]["migration_state"] == "none"
    assert url not in migrated.text
    assert config_store.load_config()["settings"].get("notification_urls") is None
    assert client.post(main.APPRISE_MIGRATION_PATH).status_code == 200


def test_migration_conflict_keeps_legacy_and_new_file(data_dir) -> None:
    legacy = "json://legacy-user:legacy-secret@localhost"
    current = "json://current-user:current-secret@localhost"
    config_store.update_config(
        lambda config: config["settings"].update(notification_urls=[legacy])
    )
    from src import secrets_store

    secrets_store.set_apprise_urls([current])
    response = TestClient(main.app).post(main.APPRISE_MIGRATION_PATH)

    assert response.status_code == 409
    assert legacy not in response.text and current not in response.text
    assert config_store.load_config()["settings"]["notification_urls"] == [legacy]
    assert main._effective_apprise_urls() == (current,)


def test_invalid_environment_value_owns_urls_and_blocks_file_mutations(
    data_dir, monkeypatch
) -> None:
    from src import secrets_store

    secrets_store.set_apprise_urls(["json://file-user:file-secret@localhost"])
    monkeypatch.setenv("APPRISE_URLS", "not-json")
    config_store.update_config(
        lambda config: config["settings"].update(
            notification_urls=["json://legacy-user:legacy-secret@localhost"]
        )
    )
    client = TestClient(main.app)

    status = client.get("/api/ui/secrets").json()["apprise-urls"]
    assert status["source"] == "environment_invalid"
    assert status["set"] is False
    assert status["migration_state"] == "conflict"
    assert main._effective_apprise_urls() == ()
    response = client.delete("/api/ui/secrets/apprise-urls")
    assert response.status_code == 409
    assert response.json() == {"detail": main.APPRISE_URLS_LOCKED}
    assert config_store.load_config()["settings"]["notification_urls"]


def test_migration_cleanup_failure_keeps_file_authoritative_and_retryable(
    data_dir, monkeypatch
) -> None:
    url = "json://retry-user:retry-secret@localhost"
    config_store.update_config(
        lambda config: config["settings"].update(notification_urls=[url])
    )
    client = TestClient(main.app)
    real_remove = main._remove_legacy_apprise_urls
    monkeypatch.setattr(
        main,
        "_remove_legacy_apprise_urls",
        lambda expected: (_ for _ in ()).throw(OSError("storage unavailable")),
    )

    failed = client.post(main.APPRISE_MIGRATION_PATH)
    assert failed.status_code == 500
    assert url not in failed.text
    assert main._effective_apprise_urls() == (url,)
    assert (
        client.get("/api/ui/secrets").json()["apprise-urls"]["migration_state"]
        == "retry"
    )

    monkeypatch.setattr(main, "_remove_legacy_apprise_urls", real_remove)
    retried = client.post(main.APPRISE_MIGRATION_PATH)
    assert retried.status_code == 200
    assert config_store.load_config()["settings"].get("notification_urls") is None


def test_test_notification_ignores_body_urls_and_uses_stored_list(
    data_dir, monkeypatch
) -> None:
    from src import secrets_store

    url = "json://stored-user:stored-secret@localhost"
    secrets_store.set_apprise_urls([url])
    sent: list[str] = []

    class FakeApprise:
        def __init__(self) -> None:
            self.servers: list[str] = []

        def add(self, value: str) -> bool:
            sent.append(value)
            self.servers.append(value)
            return True

        def notify(self, **kwargs: str) -> bool:
            return True

    monkeypatch.setattr(main.apprise, "Apprise", FakeApprise)
    response = TestClient(main.app).post(
        "/api/ui/test_notification", json={"notification_urls": ["fixture://evil"]}
    )

    assert response.status_code == 200
    assert response.json() == {"status": "success"}
    assert set(sent) == {url}


def test_invalid_apprise_put_has_fixed_error_and_does_not_persist(data_dir) -> None:
    client = TestClient(main.app)
    secret = "invalid-user:untrusted-secret"
    response = client.put(
        "/api/ui/secrets/apprise-urls", json={"value": [f"json://{secret}@localhost\n"]}
    )

    assert response.status_code == 400
    assert response.json() == {"detail": main.APPRISE_URLS_INVALID}
    assert secret not in response.text
    assert not (data_dir / ".env").exists()
    assert secrets_store.redact(secret) == secret


def test_unicode_line_separators_cannot_inject_a_github_secret(data_dir) -> None:
    known_token = "known-github-token-marker-11"
    injected_token = "FAKESECRET-token-marker-22"
    secrets_store.set_secret(secrets_store.GITHUB_TOKEN, known_token)
    separator = "\u2028"
    url = f"json://localhost?topic=alerts{separator}GITHUB_TOKEN={injected_token}"
    response = TestClient(main.app).put(
        "/api/ui/secrets/apprise-urls", json={"value": [url]}
    )

    assert response.status_code == 400
    assert secrets_store.get_secret(secrets_store.GITHUB_TOKEN) == known_token
    assert injected_token not in (data_dir / ".env").read_text()

    (data_dir / ".env").write_text(
        f"GITHUB_TOKEN={known_token}\n"
        f'APPRISE_URLS=["json://localhost?topic=alerts"]{separator}'
        f"GITHUB_TOKEN={injected_token}\n"
    )
    assert secrets_store.read_apprise_urls()[2] is False
    assert secrets_store.get_secret(secrets_store.GITHUB_TOKEN) == known_token


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85", "\x1c"])
def test_all_splitlines_separators_are_rejected_for_every_url_source(
    data_dir, monkeypatch, separator: str
) -> None:
    value = f"json://localhost?topic=first{separator}GITHUB_TOKEN=FAKESECRET-marker-33"

    with pytest.raises(secrets_store.InvalidSecretError):
        secrets_store.validate_apprise_urls([value])

    monkeypatch.setenv("APPRISE_URLS", json.dumps([value], ensure_ascii=False))
    assert secrets_store.read_apprise_urls() == (None, "environment_invalid", False)

    config_store.update_config(
        lambda config: config["settings"].update(notification_urls=[value])
    )
    monkeypatch.delenv("APPRISE_URLS")
    assert main._effective_apprise_urls() == ()


def test_json_file_encoding_escapes_unicode_line_separator_characters(data_dir) -> None:
    value = "json://localhost?title=café"
    secrets_store.set_apprise_urls([value])

    text = (data_dir / ".env").read_text(encoding="ascii")
    assert r"caf\u00e9" in text
    assert secrets_store.read_apprise_urls()[0] == (value,)


def test_invalid_apprise_file_is_a_conflict_and_is_not_overwritten(data_dir) -> None:
    legacy = "json://legacy-user:legacy-secret@localhost"
    invalid_file = b'APPRISE_URLS={"wrong":"shape"}\n# keep this byte-for-byte\n'
    (data_dir / ".env").write_bytes(invalid_file)
    config_store.update_config(
        lambda config: config["settings"].update(notification_urls=[legacy])
    )
    client = TestClient(main.app)
    assert (
        client.get("/api/ui/secrets").json()["apprise-urls"]["source"] == "file_invalid"
    )

    put = client.put(
        "/api/ui/secrets/apprise-urls",
        json={"value": ["json://new-user:new-secret@localhost"]},
    )
    migration = client.post(main.APPRISE_MIGRATION_PATH)

    assert put.status_code == 409
    assert migration.status_code == 409
    assert (data_dir / ".env").read_bytes() == invalid_file
    assert config_store.load_config()["settings"]["notification_urls"] == [legacy]


def test_one_list_entry_cannot_expand_into_multiple_apprise_destinations(
    data_dir,
) -> None:
    entry = "json://first.local json://second.local"
    response = TestClient(main.app).put(
        "/api/ui/secrets/apprise-urls", json={"value": [entry]}
    )

    assert response.status_code == 400
    assert response.json() == {"detail": main.APPRISE_URLS_INVALID}
    assert not (data_dir / ".env").exists()


def test_concurrent_explicit_migrations_are_serialized(data_dir) -> None:
    url = "json://concurrent-user:concurrent-secret@localhost"
    config_store.update_config(
        lambda config: config["settings"].update(notification_urls=[url])
    )

    def migrate() -> int:
        return TestClient(main.app).post(main.APPRISE_MIGRATION_PATH).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(lambda _: migrate(), range(2)))

    assert statuses == [200, 200]
    assert config_store.load_config()["settings"].get("notification_urls") is None
    assert secrets_store.read_apprise_urls()[0] == (url,)


def test_migration_retries_legacy_cleanup_after_post_write_read_failure(
    data_dir, monkeypatch
) -> None:
    url = "json://retry-user:retry-secret@localhost"
    config_store.update_config(
        lambda config: config["settings"].update(notification_urls=[url])
    )
    actual_set = secrets_store.set_apprise_urls
    actual_read = secrets_store.read_apprise_urls
    fail_verification_once = True
    reads = 0

    def set_then_arm(value: object) -> None:
        actual_set(value)

    def read_with_one_failure():
        nonlocal fail_verification_once, reads
        reads += 1
        if reads == 3 and fail_verification_once:
            fail_verification_once = False
            raise OSError("simulated post-replace read failure")
        return actual_read()

    monkeypatch.setattr(secrets_store, "set_apprise_urls", set_then_arm)
    monkeypatch.setattr(secrets_store, "read_apprise_urls", read_with_one_failure)
    client = TestClient(main.app)

    first = client.post(main.APPRISE_MIGRATION_PATH)
    assert first.status_code == 500
    assert config_store.load_config()["settings"]["notification_urls"] == [url]
    assert actual_read()[0] == (url,)

    retry = client.post(main.APPRISE_MIGRATION_PATH)
    assert retry.status_code == 200
    assert config_store.load_config()["settings"].get("notification_urls") is None


def test_apprise_logging_masks_transformed_credentials_in_worker_threads(
    data_dir, caplog, monkeypatch
) -> None:
    import requests

    webhook_id = "1234567890"
    webhook_token = "discord-webhook-token-value"
    url = f"discord://{webhook_id}/{webhook_token}"
    caplog.set_level(logging.DEBUG)

    def fail_without_network(*args, **kwargs):
        raise requests.ConnectionError("offline test transport")

    monkeypatch.setattr(requests, "post", fail_without_network)
    assert (
        main._send_apprise(
            (url, "discord://9876543210/another-webhook-token-value"), "test", "test"
        )
        is False
    )

    assert webhook_id not in caplog.text
    assert webhook_token not in caplog.text
    assert any("ThreadPoolExecutor" in record.threadName for record in caplog.records)
    assert secrets_store.redact(webhook_token) == "***"


def test_apprise_rotation_keeps_removed_credentials_masked_with_bounded_history(
    data_dir,
) -> None:
    first = "json://localhost?token=first-credential-token"
    shared = "json://localhost?token=shared-credential-token"
    replacement = "json://localhost?token=replacement-credential-token"

    secrets_store.remember_apprise_masks(
        main._apprise_redaction_values((first, shared))
    )
    secrets_store.remember_apprise_masks(
        main._apprise_redaction_values((shared, replacement))
    )

    assert secrets_store.redact("first-credential-token") == "***"
    assert secrets_store.redact("shared-credential-token") == "***"
    assert secrets_store.redact("replacement-credential-token") == "***"

    for index in range(secrets_store.MAX_RETIRED_APPRISE_MASK_SETS + 2):
        secrets_store.remember_apprise_masks(
            main._apprise_redaction_values(
                (f"json://localhost?token=rotated-credential-token-{index:02d}",)
            )
        )
    assert (
        len(secrets_store._apprise_mask_retired)
        <= secrets_store.MAX_RETIRED_APPRISE_MASK_SETS
    )


def test_apprise_masking_does_not_register_arbitrary_query_values(data_dir) -> None:
    url = "json://localhost?topic=ordinary-common-value"
    masks = main._apprise_redaction_values((url,))
    secrets_store.remember_apprise_masks(masks)

    assert url in masks
    assert "ordinary-common-value" not in masks
    assert secrets_store.redact("ordinary-common-value") == "ordinary-common-value"


def test_kavenegar_private_metadata_masks_api_key_during_successful_worker_notify(
    data_dir, caplog, monkeypatch
) -> None:
    import requests

    apikey = "fakekavenegarapikey1234567890"
    url = f"kavenegar://{apikey}/09123456789"
    caplog.set_level(logging.DEBUG)

    class Response:
        status_code = 200
        content = b'{"return":{"status":200}}'

    def successful_local_transport(*args, **kwargs):
        logging.getLogger("test-transport").debug("transport key=%s", apikey)
        return Response()

    monkeypatch.setattr(requests, "post", successful_local_transport)
    assert main._send_apprise((url,), "title", "body") is True
    assert apikey not in caplog.text


def test_active_worker_masks_survive_more_than_retired_capacity_rotations(
    data_dir, caplog, monkeypatch
) -> None:
    from threading import Event, Thread

    entered = Event()
    release = Event()
    credential = "blocked-kavenegar-apikey-123456"
    url = f"kavenegar://{credential}/123456789"
    caplog.set_level(logging.DEBUG)

    class BlockingApprise:
        def __init__(self) -> None:
            self.servers: list[str] = []

        def add(self, value: str) -> bool:
            self.servers.append(value)
            return True

        def notify(self, **kwargs: str) -> bool:
            entered.set()
            assert release.wait(timeout=5)
            logging.getLogger("test-worker").warning("active credential %s", credential)
            return True

    monkeypatch.setattr(main.apprise, "Apprise", BlockingApprise)
    result: list[bool] = []
    worker = Thread(target=lambda: result.append(main._send_apprise((url,), "t", "b")))
    worker.start()
    assert entered.wait(timeout=5)

    for index in range(secrets_store.MAX_RETIRED_APPRISE_MASK_SETS + 4):
        secrets_store.remember_apprise_masks((f"rotated-key-value-{index:02d}",))
    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert result == [True]
    assert credential not in caplog.text


def test_apprise_parser_rejects_bad_url_without_remembering_guessed_credentials(
    data_dir, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    rejected = "madeupscheme://fakeknowncredential123456/"

    assert main._apprise_urls_are_supported((rejected,)) is False
    assert (
        secrets_store.redact("fakeknowncredential123456") == "fakeknowncredential123456"
    )
    assert rejected not in caplog.text


@pytest.mark.parametrize("source", ["environment", "file", "legacy"])
def test_secrets_status_registers_effective_credentials_before_logs_are_viewed(
    data_dir, monkeypatch, source: str
) -> None:
    credential = "fakeeffectiveapikey1234567890"
    url = f"kavenegar://{credential}/09123456789"
    if source == "environment":
        monkeypatch.setenv("APPRISE_URLS", json.dumps([url]))
    elif source == "file":
        secrets_store.set_apprise_urls([url])
    else:
        config_store.update_config(
            lambda config: config["settings"].update(notification_urls=[url])
        )
    main.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    main.LOG_PATH.write_text(f"Apprise diagnostic: {credential}\n")

    response = TestClient(main.app).get("/api/ui/secrets")
    logs = TestClient(main.app).get("/api/ui/logs").json()["logs"]

    assert response.status_code == 200
    assert credential not in logs


def test_active_mask_admission_failure_does_not_leak_or_hold_registry_lock(
    data_dir,
) -> None:
    saturated = tuple(
        f"m-{index:05d}-xxxx" for index in range(secrets_store.MAX_APPRISE_MASK_VALUES)
    )
    with (
        secrets_store.active_apprise_masks(saturated),
        pytest.raises(secrets_store.AppriseRedactionCapacityError),
        secrets_store.active_apprise_masks(("one-more-mask-value",)),
    ):
        pytest.fail("capacity admission should reject before entering")
    assert not secrets_store._active_apprise_masks
    assert secrets_store.redact("one-more-mask-value") == "one-more-mask-value"


def test_active_mask_pin_is_released_after_delivery_exception(
    data_dir, monkeypatch, caplog
) -> None:
    credential = "exception-kavenegar-apikey-123456"
    url = f"kavenegar://{credential}/09123456789"

    class RaisingApprise:
        def __init__(self) -> None:
            self.servers: list[str] = []

        def add(self, value: str) -> bool:
            self.servers.append(value)
            return True

        def notify(self, **kwargs: str) -> bool:
            raise RuntimeError("simulated notifier failure")

    monkeypatch.setattr(main.apprise, "Apprise", RaisingApprise)

    assert main._send_apprise((url,), "title", "body") is False

    assert not secrets_store._active_apprise_masks
    assert credential not in caplog.text
