"""An environment secret is checked like a file secret before it is used."""

import logging

import pytest
from fastapi.testclient import TestClient

from src import main, secrets_store

GITHUB = secrets_store.GITHUB_TOKEN
FAKE_FILE_TOKEN = "fake-file-token-for-tests-0003"
INVALID_VALUES = [
    "fake-env\x07-token-0004",
    "fake env token 0005",
    "fake-env-token-0006\nOTHER=1",
    "short",
    "fake'quoted'token-0007",
    "x" * 513,
    "fake-töken-for-tests-0008",
]


@pytest.fixture(autouse=True)
def fresh_warning_state():
    secrets_store._warned_invalid_env.clear()
    yield
    secrets_store._warned_invalid_env.clear()


@pytest.mark.parametrize("value", INVALID_VALUES, ids=range(len(INVALID_VALUES)))
def test_invalid_environment_value_is_never_returned(monkeypatch, value):
    monkeypatch.setenv(GITHUB, value)

    assert secrets_store.get_secret(GITHUB) is None


@pytest.mark.parametrize("value", INVALID_VALUES, ids=range(len(INVALID_VALUES)))
def test_invalid_environment_value_is_reported_as_invalid(monkeypatch, value):
    monkeypatch.setenv(GITHUB, value)

    assert secrets_store.secret_source(GITHUB) == "environment_invalid"
    assert secrets_store.get_status() == {GITHUB: "environment_invalid"}


def test_valid_environment_value_still_wins(monkeypatch):
    monkeypatch.setenv(GITHUB, "  fake-env-token-0009  ")

    assert secrets_store.get_secret(GITHUB) == "fake-env-token-0009"
    assert secrets_store.secret_source(GITHUB) == "environment"


def test_invalid_environment_value_does_not_fall_back_to_the_file(
    isolated_data_dir, monkeypatch
):
    secrets_store.set_secret(GITHUB, FAKE_FILE_TOKEN)
    monkeypatch.setenv(GITHUB, "bad value")

    assert secrets_store.get_secret(GITHUB) is None
    assert secrets_store.secret_source(GITHUB) == "environment_invalid"


def test_invalid_environment_value_still_locks_the_file(monkeypatch):
    monkeypatch.setenv(GITHUB, "bad value")

    with pytest.raises(secrets_store.SecretFromEnvironmentError):
        secrets_store.set_secret(GITHUB, FAKE_FILE_TOKEN)
    with pytest.raises(secrets_store.SecretFromEnvironmentError):
        secrets_store.clear_secret(GITHUB)


def test_one_warning_names_the_variable_and_never_the_value(monkeypatch, caplog):
    value = "fake-env\x07-token-0010"
    monkeypatch.setenv(GITHUB, value)

    with caplog.at_level(logging.WARNING, logger="src.secrets_store"):
        for _ in range(3):
            secrets_store.get_secret(GITHUB)
            secrets_store.get_status()

    warnings = [r for r in caplog.records if r.name == "src.secrets_store"]
    assert len(warnings) == 1
    assert GITHUB in warnings[0].getMessage()
    assert "invalid" in warnings[0].getMessage().lower()
    assert value not in caplog.text and "token-0010" not in caplog.text


def test_warning_comes_back_after_the_variable_was_fixed_and_broken_again(
    monkeypatch, caplog
):
    caplog.set_level(logging.WARNING, logger="src.secrets_store")
    monkeypatch.setenv(GITHUB, "bad value")
    secrets_store.get_secret(GITHUB)
    monkeypatch.setenv(GITHUB, "fake-env-token-0011")
    secrets_store.get_secret(GITHUB)
    monkeypatch.setenv(GITHUB, "bad value again")
    secrets_store.get_secret(GITHUB)

    assert len([r for r in caplog.records if GITHUB in r.getMessage()]) == 2


@pytest.fixture
def client(isolated_data_dir):
    (isolated_data_dir / "apks" / "icons").mkdir(parents=True)
    return TestClient(main.app)


def test_status_route_tells_the_operator_the_variable_is_invalid(client, monkeypatch):
    value = "fake-env\x07-token-0012"
    monkeypatch.setenv(GITHUB, value)

    response = client.get("/api/ui/secrets")

    assert response.status_code == 200
    assert response.json()["github-token"] == {
        "set": False,
        "source": "environment_invalid",
    }
    assert "token-0012" not in response.text


def test_put_is_refused_while_the_environment_owns_the_name(client, monkeypatch):
    monkeypatch.setenv(GITHUB, "bad value")

    response = client.put(
        "/api/ui/secrets/github-token", json={"value": FAKE_FILE_TOKEN}
    )

    assert response.status_code == 409
