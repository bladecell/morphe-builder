"""The server URL as a trust anchor, and an empty server URL on save.

Decided 2026-10-06: the server URL stays a trust anchor until `ALLOWED_HOSTS`
lists at least one full origin, and an empty server URL can be saved.
"""

import pytest
from fastapi import HTTPException

from src import main, request_guards


@pytest.mark.parametrize(
    "allowed_hosts",
    [None, "", ",", "upstream", "upstream:8000", "upstream, nas.local", "bad host"],
)
def test_server_url_is_kept_without_a_full_origin(allowed_hosts: str | None) -> None:
    """Bare names, blank text and junk do not replace the server URL."""
    assert request_guards.server_url_is_ignored(allowed_hosts) is False


@pytest.mark.parametrize(
    "allowed_hosts",
    ["https://m.example", "upstream, https://m.example", "http://nas.local:8000/"],
)
def test_server_url_is_ignored_once_a_full_origin_is_listed(
    allowed_hosts: str,
) -> None:
    """One full origin makes `ALLOWED_HOSTS` the only trust anchor."""
    assert request_guards.server_url_is_ignored(allowed_hosts) is True


def test_proxy_with_a_bare_name_keeps_its_writes() -> None:
    """A rewritten Host in `ALLOWED_HOSTS` plus the public server URL works."""
    allowed = request_guards.is_origin_allowed(
        "https://m.example", "upstream:8000", "https://m.example", "upstream"
    )

    assert allowed is True


def test_full_origin_entry_drops_the_server_url_as_origin() -> None:
    """With a full origin listed, the server URL no longer allows an Origin."""
    allowed = request_guards.is_origin_allowed(
        "https://evil.example",
        "upstream:8000",
        "https://evil.example",
        "upstream, https://m.example",
    )

    assert allowed is False


def test_empty_server_url_can_be_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty server URL is accepted as unset, replacing a stored one."""
    saved: dict = {"settings": {"server_url": "http://old.example:8000"}}
    monkeypatch.setattr(main, "load_config", lambda: saved)
    monkeypatch.setattr(main, "update_config", lambda apply: apply(saved))
    monkeypatch.setattr(main, "setup_cron_job", lambda config: None)
    payload = main.SaveSettingsPayload(
        server_url="", cron_schedule="0 4 * * *", notification_urls=[]
    )

    main.save_settings(payload)

    assert saved["settings"]["server_url"] == ""


def test_invalid_changed_server_url_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed value that is not a URL still answers 400."""
    monkeypatch.setattr(main, "load_config", lambda: {"settings": {}})
    payload = main.SaveSettingsPayload(
        server_url="not a url", cron_schedule="0 4 * * *", notification_urls=[]
    )

    with pytest.raises(HTTPException) as raised:
        main.save_settings(payload)

    assert raised.value.status_code == 400
