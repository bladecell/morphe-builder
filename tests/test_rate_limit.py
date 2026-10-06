import time

import pytest
import requests
import yaml
from fastapi import HTTPException

from src import config_store, main, patcher

RESET_EPOCH = 1_900_000_000
RESET_TEXT = "2030-03-17 17:46 UTC"


class FakeResponse:
    is_redirect = False
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} Client Error")


def limited_response():
    return FakeResponse(
        403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(RESET_EPOCH)}
    )


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    monkeypatch.setattr(patcher, "DATA_DIR", tmp_path)
    monkeypatch.setattr(patcher, "load_local_versions", dict)
    return {".json": tmp_path / "f.json"}


@pytest.mark.parametrize("status", [403, 429])
def test_primary_limit_raises_with_reset_time(sync_env, monkeypatch, status):
    response = FakeResponse(
        status, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(RESET_EPOCH)}
    )
    monkeypatch.setattr(patcher.requests, "get", lambda *a, **k: response)
    with pytest.raises(patcher.GitHubRateLimitError) as exc:
        patcher.check_and_download_release("a/b", sync_env, force=True)
    assert "rate limit" in str(exc.value)
    assert RESET_TEXT in str(exc.value)


def test_secondary_limit_uses_retry_after(sync_env, monkeypatch):
    response = FakeResponse(403, {"retry-after": "120"})
    monkeypatch.setattr(patcher.requests, "get", lambda *a, **k: response)
    with pytest.raises(patcher.GitHubRateLimitError) as exc:
        patcher.check_and_download_release("a/b", sync_env, force=True)
    assert exc.value.reset_epoch == pytest.approx(time.time() + 120, abs=5)


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(403),
        FakeResponse(403, {"x-ratelimit-remaining": "42"}),
        FakeResponse(500),
        FakeResponse(401, {"x-ratelimit-remaining": "0"}),
    ],
)
def test_other_http_failures_are_not_rate_limits(sync_env, monkeypatch, response):
    monkeypatch.setattr(patcher.requests, "get", lambda *a, **k: response)
    assert patcher.check_and_download_release("a/b", sync_env, force=True) is False


def test_update_tools_stops_and_raises_on_rate_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(patcher, "DATA_DIR", tmp_path)
    monkeypatch.setattr(patcher, "load_local_versions", dict)
    repos = []

    def fake_check(repo, paths, force=False):
        repos.append(repo)
        raise patcher.GitHubRateLimitError(RESET_EPOCH)

    monkeypatch.setattr(patcher, "check_and_download_release", fake_check)
    config = {
        "repositories": {"cli": "x/cli"},
        "tools": {"cli_jar": "cli.jar"},
        "sources": [{"name": "s", "repo": "a/b", "active": True}],
    }
    with pytest.raises(patcher.GitHubRateLimitError):
        patcher.update_tools(config, quiet=True)
    assert repos == ["x/cli"]


def test_update_tools_saves_progress_before_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(patcher, "DATA_DIR", tmp_path)
    monkeypatch.setattr(patcher, "load_local_versions", lambda: {"x/cli": "v2"})
    monkeypatch.setattr(config_store, "DATA_DIR", tmp_path)

    def fake_check(repo, paths, force=False):
        if repo == "x/cli":
            return True
        raise patcher.GitHubRateLimitError(RESET_EPOCH)

    monkeypatch.setattr(patcher, "check_and_download_release", fake_check)
    config = {
        "repositories": {"cli": "x/cli"},
        "tools": {"cli_jar": "cli.jar"},
        "sources": [{"name": "s", "repo": "a/b", "active": True}],
    }
    config_store.save_config(config)
    with pytest.raises(patcher.GitHubRateLimitError):
        patcher.update_tools(config, quiet=True)
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert saved["tools"]["cli_version"] == "v2"
    assert config["tools"]["cli_version"] == "v2"


def test_pipeline_logs_rate_limit_and_continues(monkeypatch, capsys):
    monkeypatch.setattr(patcher, "load_config", lambda: {"apps": []})

    def limited(config, quiet=False, force=False):
        raise patcher.GitHubRateLimitError(RESET_EPOCH)

    reached = []
    monkeypatch.setattr(patcher, "update_tools", limited)
    monkeypatch.setattr(
        patcher, "get_patch_versions", lambda cfg: reached.append(1) or {}
    )
    patcher.run_pipeline(quiet=True, scraper=object())
    out = capsys.readouterr().out
    assert "rate limit" in out
    assert RESET_TEXT in out
    assert reached == [1]


@pytest.fixture
def add_source_env(tmp_path, monkeypatch):
    store = {"sources": []}
    paths = {".mpp": tmp_path / "p.mpp", ".json": tmp_path / "p.json"}
    monkeypatch.setattr(main, "load_config", lambda: store)
    monkeypatch.setattr(main, "update_config", lambda mutate: mutate(store))
    monkeypatch.setattr(main, "get_source_paths", lambda repo: paths)
    return store


def test_add_source_reports_rate_limit(add_source_env, monkeypatch):
    def limited(*args, **kwargs):
        raise patcher.GitHubRateLimitError(RESET_EPOCH)

    monkeypatch.setattr(main, "check_and_download_release", limited)
    with pytest.raises(HTTPException) as exc:
        main.add_source(main.AddSourcePayload(repo="new/repo"))
    assert exc.value.status_code == 429
    assert "rate limit" in exc.value.detail
    assert RESET_TEXT in exc.value.detail
    assert add_source_env["sources"] == []


def test_sync_task_exposes_and_clears_rate_limit(monkeypatch):
    outcomes = [patcher.GitHubRateLimitError(RESET_EPOCH), None]

    def fake_update(config, quiet=False, force=False):
        outcome = outcomes.pop(0)
        if outcome:
            raise outcome
        return True

    monkeypatch.setattr(main, "update_tools", fake_update)
    main.update_tools_task({}, True)
    status = main.get_tools_status()
    assert "rate limit" in status["sync_error"]
    assert status["updating"] is False
    main.update_tools_task({}, True)
    assert main.get_tools_status()["sync_error"] is None
