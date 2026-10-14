"""Changing the GitHub token schedules one background tool sync.

The routes answer with an `X-Sync` header (the body stays the status dict):
`queued`, `already-running`, `not-needed` (the stored token did not change) or
`unavailable` (the sync could not be scheduled). The header is absent for other
secrets and for refused or failed writes.
"""

import pytest
from fastapi.testclient import TestClient

from src import main

pytestmark = pytest.mark.real_sync_scheduling

TOKEN_VALUE = "fake-token-for-tests-0001"
OTHER_VALUE = "fake-token-for-tests-0002"
GITHUB = "/api/ui/secrets/github-token"


class RecordingScheduler:
    """Stands in for the APScheduler instance; records `add_job` calls."""

    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.error: Exception | None = None

    def add_job(self, func, **kwargs) -> None:
        if self.error is not None:
            raise self.error
        self.jobs.append({"func": func, **kwargs})


@pytest.fixture
def scheduler(monkeypatch) -> RecordingScheduler:
    fake = RecordingScheduler()
    monkeypatch.setattr(main, "scheduler", fake)
    return fake


@pytest.fixture
def client(isolated_data_dir, scheduler):
    (isolated_data_dir / "apks" / "icons").mkdir(parents=True)
    return TestClient(main.app)


def sync_header(response) -> str | None:
    return response.headers.get("X-Sync")


def test_setting_the_token_schedules_one_forced_claimed_sync(client, scheduler):
    response = client.put(GITHUB, json={"value": TOKEN_VALUE})

    assert response.status_code == 200
    assert sync_header(response) == "queued"
    assert len(scheduler.jobs) == 1
    job = scheduler.jobs[0]
    assert job["func"] is main.update_tools_task
    assert job["args"][1:] == [True, True]
    assert isinstance(job["args"][0], dict)
    assert job["misfire_grace_time"] is None
    assert main.TOOLS_UPDATING is True


def test_the_body_keeps_the_status_shape(client):
    body = client.put(GITHUB, json={"value": TOKEN_VALUE}).json()

    assert set(body) == {
        "github-token",
        "apprise-urls",
    }


def test_replacing_the_token_with_a_new_value_schedules_a_sync(client, scheduler):
    client.put(GITHUB, json={"value": TOKEN_VALUE})
    main.TOOLS_UPDATING = False

    response = client.put(GITHUB, json={"value": OTHER_VALUE})

    assert sync_header(response) == "queued"
    assert len(scheduler.jobs) == 2


def test_saving_the_same_token_again_schedules_nothing(client, scheduler):
    client.put(GITHUB, json={"value": TOKEN_VALUE})
    main.TOOLS_UPDATING = False
    scheduler.jobs.clear()

    response = client.put(GITHUB, json={"value": TOKEN_VALUE})

    assert response.status_code == 200
    assert sync_header(response) == "not-needed"
    assert scheduler.jobs == []
    assert main.TOOLS_UPDATING is False


def test_clearing_a_stored_token_schedules_a_sync(client, scheduler):
    client.put(GITHUB, json={"value": TOKEN_VALUE})
    main.TOOLS_UPDATING = False
    scheduler.jobs.clear()

    response = client.delete(GITHUB)

    assert response.status_code == 200
    assert sync_header(response) == "queued"
    assert len(scheduler.jobs) == 1


def test_clearing_when_no_token_is_stored_schedules_nothing(client, scheduler):
    response = client.delete(GITHUB)

    assert response.status_code == 200
    assert sync_header(response) == "not-needed"
    assert scheduler.jobs == []
    assert main.TOOLS_UPDATING is False


def test_an_invalid_value_schedules_nothing(client, scheduler):
    response = client.put(GITHUB, json={"value": "short"})

    assert response.status_code == 400
    assert sync_header(response) is None
    assert scheduler.jobs == []
    assert main.TOOLS_UPDATING is False


def test_a_token_set_by_the_environment_schedules_nothing(
    client, scheduler, monkeypatch
):
    monkeypatch.setenv("GITHUB_TOKEN", "env-token-for-tests-0004")

    put = client.put(GITHUB, json={"value": TOKEN_VALUE})
    delete = client.delete(GITHUB)

    assert (put.status_code, delete.status_code) == (409, 409)
    assert scheduler.jobs == []
    assert main.TOOLS_UPDATING is False


def test_a_failed_write_schedules_nothing(client, scheduler, monkeypatch):
    def fail(name: str, value: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(main.secrets_store, "set_secret", fail)

    response = client.put(GITHUB, json={"value": TOKEN_VALUE})

    assert response.status_code == 500
    assert sync_header(response) is None
    assert scheduler.jobs == []
    assert main.TOOLS_UPDATING is False


def test_a_running_sync_is_reported_and_not_doubled(client, scheduler, monkeypatch):
    monkeypatch.setattr(main, "TOOLS_UPDATING", True)

    response = client.put(GITHUB, json={"value": TOKEN_VALUE})

    assert response.status_code == 200
    assert sync_header(response) == "already-running"
    assert response.json()["github-token"] == {"set": True, "source": "file"}
    assert scheduler.jobs == []


@pytest.mark.parametrize("content", ["", "- just\n- a list\n", "key: [unclosed"])
def test_an_unusable_config_is_reported_and_keeps_the_saved_token(
    client, scheduler, isolated_data_dir, content
):
    (isolated_data_dir / "config.yaml").write_text(content)

    response = client.put(GITHUB, json={"value": TOKEN_VALUE})

    assert response.status_code == 200
    assert sync_header(response) == "unavailable"
    assert response.json()["github-token"]["set"] is True
    assert scheduler.jobs == []
    assert main.TOOLS_UPDATING is False


def test_a_scheduler_failure_releases_the_claim(client, scheduler):
    scheduler.error = RuntimeError("scheduler is shut down")

    response = client.put(GITHUB, json={"value": TOKEN_VALUE})

    assert response.status_code == 200
    assert sync_header(response) == "unavailable"
    assert main.TOOLS_UPDATING is False


def test_scheduling_does_not_depend_on_the_response_being_delivered(client, scheduler):
    # The job is handed to the scheduler before the handler returns, so a client
    # that disconnects (which can skip Starlette background tasks) cannot leave
    # the claim without an owner.
    client.put(GITHUB, json={"value": TOKEN_VALUE})

    assert len(scheduler.jobs) == 1
