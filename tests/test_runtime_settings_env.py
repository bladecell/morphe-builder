from __future__ import annotations

import asyncio

import pytest
import yaml
from starlette.requests import Request

from src import config_store, main, request_guards


def test_unset_environment_keeps_saved_settings(data_dir, monkeypatch):
    monkeypatch.delenv("SERVER_URL", raising=False)
    monkeypatch.delenv("CRON_SCHEDULE", raising=False)
    settings = config_store.load_config()["settings"]
    assert settings["server_url"] == "http://x"
    assert settings["cron_schedule"] == "0 * * * *"


def test_environment_settings_override_runtime_without_persisting(
    data_dir, monkeypatch
):
    monkeypatch.setenv("SERVER_URL", "https://public.example/path")
    monkeypatch.setenv("CRON_SCHEDULE", "")

    effective = config_store.load_config()["settings"]
    assert effective["server_url"] == "https://public.example/path"
    assert effective["cron_schedule"] == ""

    config_store.update_config(lambda config: config["settings"].update(theme="dark"))
    saved = yaml.safe_load((data_dir / "config.yaml").read_text())["settings"]
    assert saved["server_url"] == "http://x"
    assert saved["cron_schedule"] == "0 * * * *"
    assert (
        config_store.load_config()["settings"]["server_url"]
        == "https://public.example/path"
    )


def test_request_guard_uses_environment_server_url(monkeypatch):
    monkeypatch.setenv("SERVER_URL", "https://public.example/path/")
    server_url = request_guards.read_server_url()
    assert server_url == "https://public.example/path"
    assert config_store.runtime_environment_settings()["server_url"] == server_url
    assert "public.example" in request_guards.allowed_host_names(server_url, None)
    assert request_guards.is_origin_allowed(
        "https://public.example", "proxy.internal", server_url
    )


def test_obtainium_api_urls_use_environment_server_url(data_dir, monkeypatch):
    monkeypatch.setenv("SERVER_URL", "https://public.example/root")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/apps",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1),
            "query_string": b"",
            "root_path": "",
        }
    )

    response = main.get_apps(request)
    assert all(
        app["download_url"].startswith("https://public.example/root/download/")
        for app in response["apps"]
    )
    assert response["apps"]


def test_environment_settings_render_as_readonly(data_dir, monkeypatch):
    monkeypatch.setenv("SERVER_URL", "https://public.example")
    monkeypatch.setenv("CRON_SCHEDULE", "")
    page = main.templates.get_template("index.html").render(
        config=config_store.load_config(),
        available_apps=[],
        metadata={},
        environment_settings={"server_url": True, "cron_schedule": True},
        request=None,
    )
    assert 'id="settings-server-url"' in page
    assert 'id="settings-server-url" type="text"' in page
    assert 'readonly aria-readonly="true"' in page
    assert "Controlled by SERVER_URL" in page
    assert "Controlled by CRON_SCHEDULE" in page


def test_empty_environment_cron_disables_scheduled_jobs(data_dir, monkeypatch):
    monkeypatch.setenv("CRON_SCHEDULE", "")
    added = []

    class Scheduler:
        def remove_job(self, job_id):
            return None

        def add_job(self, *args, **kwargs):
            added.append(kwargs)

    monkeypatch.setattr(main, "scheduler", Scheduler())
    main.setup_cron_job(config_store.load_config())
    assert added == []


@pytest.mark.parametrize(
    "name,value",
    [("SERVER_URL", ""), ("SERVER_URL", "ftp://example.com"), ("CRON_SCHEDULE", "bad")],
)
def test_invalid_environment_settings_are_rejected(name, value, monkeypatch):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        main.validate_environment_settings()


def test_invalid_environment_setting_stops_startup_before_worker(monkeypatch):
    monkeypatch.setenv("SERVER_URL", "")
    started = False

    def start_worker():
        nonlocal started
        started = True

    monkeypatch.setattr(main.SCRAPER_WORKER, "start", start_worker)
    with pytest.raises(ValueError, match="Invalid SERVER_URL"):
        asyncio.run(main.startup_event())
    assert not started


def test_save_settings_does_not_replace_environment_controlled_fields(
    data_dir, monkeypatch
):
    monkeypatch.setenv("SERVER_URL", "https://public.example")
    monkeypatch.setenv("CRON_SCHEDULE", "")
    monkeypatch.setattr(main, "setup_cron_job", lambda config: None)

    main.save_settings(
        main.SaveSettingsPayload(
            server_url="https://posted.example",
            cron_schedule="0 1 * * *",
        )
    )

    persisted = yaml.safe_load((data_dir / "config.yaml").read_text())["settings"]
    assert persisted["server_url"] == "http://x"
    assert persisted["cron_schedule"] == "0 * * * *"
