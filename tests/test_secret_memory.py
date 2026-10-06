"""Only a value that is really in effect is remembered, and the memory is bounded."""

import pytest
from fastapi.testclient import TestClient

from src import config_store, main, secrets_store

GITHUB = secrets_store.GITHUB_TOKEN
LICENCE = secrets_store.CLOAKBROWSER_LICENSE_KEY
ENV_TOKEN = "env-token-for-tests-9001"
WORD = "workflow"  # an ordinary word that passes the secret pattern


@pytest.fixture(autouse=True)
def fresh_memory(monkeypatch):
    """Nothing remembered, captured or warned about at the start of a test."""
    monkeypatch.setattr(secrets_store, "_captured_environment", {}, raising=False)
    monkeypatch.setattr(secrets_store, "_warned_invalid_env", set())


def count_known() -> int:
    return len(secrets_store._known)


def test_a_put_refused_because_the_environment_owns_the_name_is_not_remembered(
    monkeypatch,
):
    monkeypatch.setenv(GITHUB, ENV_TOKEN)

    with pytest.raises(secrets_store.SecretFromEnvironmentError):
        secrets_store.set_secret(GITHUB, WORD)

    assert secrets_store.redact(f"a {WORD} here") == f"a {WORD} here"


def test_a_put_that_fails_to_write_is_not_remembered(monkeypatch):
    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(config_store, "write_file_atomic", broken)

    with pytest.raises(OSError):
        secrets_store.set_secret(GITHUB, WORD)

    assert secrets_store.redact(WORD) == WORD


def test_a_failure_message_that_holds_the_new_value_is_cleaned(monkeypatch):
    value = "value-in-an-error-message-77"

    def broken(*args: object, **kwargs: object) -> None:
        raise OSError(28, f"No space left while writing {value}")

    monkeypatch.setattr(config_store, "write_file_atomic", broken)

    with pytest.raises(OSError) as raised:
        secrets_store.set_secret(GITHUB, value)

    assert value not in str(raised.value)
    assert value not in repr(raised.value.args)
    assert raised.value.errno == 28


def test_the_new_value_is_masked_while_it_is_being_written(monkeypatch):
    value = "masked-during-the-write-55"
    seen: list[str] = []
    real_write = config_store.write_file_atomic

    def spying(*args: object, **kwargs: object) -> None:
        seen.append(secrets_store.redact(f"writing {value}"))
        real_write(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(config_store, "write_file_atomic", spying)

    secrets_store.set_secret(GITHUB, value)

    assert seen == ["writing ***"]


def test_a_written_value_is_remembered_and_stays_masked_after_rotation():
    secrets_store.set_secret(GITHUB, "first-real-token-0001")
    secrets_store.set_secret(GITHUB, "second-real-token-0002")

    masked = secrets_store.redact("first-real-token-0001 second-real-token-0002")

    assert masked == "*** ***"


def test_cleared_value_stays_masked():
    secrets_store.set_secret(GITHUB, "cleared-real-token-0003")
    secrets_store.clear_secret(GITHUB)

    assert secrets_store.redact("cleared-real-token-0003") == "***"


def test_refused_puts_never_grow_the_memory(monkeypatch):
    monkeypatch.setenv(GITHUB, ENV_TOKEN)
    secrets_store.redact("")  # the environment value is in effect: remembered
    before = count_known()

    for number in range(300):
        with pytest.raises(secrets_store.SecretFromEnvironmentError):
            secrets_store.set_secret(GITHUB, f"refused-value-{number:05d}")

    assert count_known() == before


def test_the_memory_has_a_cap_even_for_values_that_were_written():
    for number in range(300):
        secrets_store.set_secret(LICENCE, f"rotated-value-{number:05d}")

    assert count_known() <= secrets_store.MAX_REMEMBERED_VALUES


def test_values_in_effect_are_never_evicted_by_a_flood_of_rotations(monkeypatch):
    monkeypatch.setenv(GITHUB, ENV_TOKEN)
    secrets_store.absorb_environment()
    for number in range(300):
        secrets_store.set_secret(LICENCE, f"rotated-value-{number:05d}")

    assert secrets_store.redact(ENV_TOKEN) == "***"
    assert secrets_store.redact("rotated-value-00299") == "***"  # the one in effect


def test_put_route_that_ends_in_409_masks_nothing(isolated_data_dir, monkeypatch):
    (isolated_data_dir / "apks" / "icons").mkdir(parents=True)
    monkeypatch.setenv(GITHUB, ENV_TOKEN)
    client = TestClient(main.app)

    response = client.put("/api/ui/secrets/github-token", json={"value": WORD})

    assert response.status_code == 409
    assert secrets_store.redact(WORD) == WORD
