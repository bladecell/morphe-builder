"""One refresh at a time for the two polled GET routes, and no GET config write."""

import threading

import pytest

from src import config_store, main, patcher

WAIT = 5


class GateWorker:
    """Scraper worker stand-in: blocks each call until released, counts calls."""

    def __init__(self, fail: bool = False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.fail = fail
        self.versions = ["1.0"]

    def execute(self, func, *args, **kwargs):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(WAIT)
        if self.fail:
            raise RuntimeError("scraper down")
        return (self.versions, None, None)


@pytest.fixture
def updates_env(data_dir, monkeypatch):
    worker = GateWorker()
    monkeypatch.setattr(main, "SCRAPER_WORKER", worker)
    monkeypatch.setattr(main, "UPDATE_CACHE", {"timestamp": 0, "results": None})
    monkeypatch.setattr(main, "SCRAPER_ACTIVE", False)
    monkeypatch.setattr(main, "BUILD_STATUS", {"in_progress": False})
    monkeypatch.setattr(patcher, "get_patch_versions", lambda cfg: {})
    return worker


def in_thread(func):
    """Run `func` in a thread; return (thread, results list, errors list)."""
    results, errors = [], []

    def run():
        try:
            results.append(func())
        except BaseException as error:  # noqa: BLE001 - surfaced by the asserts
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, results, errors


# --- /api/ui/updates -----------------------------------------------------------


def test_updates_runs_one_refresh_while_another_is_in_progress(updates_env):
    worker = updates_env
    first, first_results, first_errors = in_thread(main.check_updates)
    assert worker.entered.wait(WAIT)

    # The first refresh is parked inside the scraper; these must not queue behind it.
    during = [main.check_updates() for _ in range(5)]
    calls_during = worker.calls
    worker.release.set()
    first.join(WAIT)
    assert not first.is_alive()

    assert not first_errors
    assert during == [{}] * 5
    assert calls_during == 1
    assert set(first_results[0]) == {"com.a", "com.b"}
    assert worker.calls == 2  # one refresh: one lookup per unpinned app


def test_updates_in_progress_answer_is_the_stale_result_when_there_is_one(
    updates_env, monkeypatch
):
    stale = {"com.a": {"has_update": False}}
    monkeypatch.setattr(main, "UPDATE_CACHE", {"timestamp": 1, "results": stale})
    worker = updates_env
    first, _, _ = in_thread(main.check_updates)
    assert worker.entered.wait(WAIT)

    during = main.check_updates()
    worker.release.set()
    first.join(WAIT)
    assert not first.is_alive()

    assert during == stale
    assert worker.calls == 2


def test_updates_fresh_cache_is_still_served_without_a_refresh(updates_env):
    updates_env.release.set()
    main.check_updates()

    again = main.check_updates()

    assert set(again) == {"com.a", "com.b"}
    assert updates_env.calls == 2


def test_updates_request_that_waited_for_the_lock_reuses_the_fresh_result(
    updates_env, monkeypatch
):
    # A request that passed the freshness check just before a refresh finished must
    # not run a second refresh once it gets the lock.
    updates_env.release.set()
    main.check_updates()
    monkeypatch.setattr(main, "UPDATE_CACHE", dict(main.UPDATE_CACHE))
    main.UPDATE_CACHE["timestamp"] = 0  # looks stale on the first check
    original_acquire = main.UPDATE_LOCK.acquire

    def refreshed_meanwhile(*args, **kwargs):
        main.UPDATE_CACHE["timestamp"] = main.time.time()
        return original_acquire(*args, **kwargs)

    monkeypatch.setattr(
        main, "UPDATE_LOCK", _LockSpy(main.UPDATE_LOCK, refreshed_meanwhile)
    )

    main.check_updates()

    assert updates_env.calls == 2


class _LockSpy:
    def __init__(self, lock, acquire):
        self._lock = lock
        self.acquire = acquire

    def release(self):
        self._lock.release()


def test_updates_lock_is_released_after_a_failed_refresh(updates_env):
    updates_env.fail = True
    updates_env.release.set()
    degraded = main.check_updates()  # a failed lookup degrades, it does not raise
    assert degraded["com.a"]["status"] == "Error"

    updates_env.fail = False
    main.UPDATE_CACHE = {"timestamp": 0, "results": None}
    result = main.check_updates()

    assert set(result) == {"com.a", "com.b"}


# --- the cache is stamped when the refresh ends -----------------------------------


class SlowClockWorker(GateWorker):
    """Each lookup takes 100 fake seconds."""

    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.release.set()

    def execute(self, func, *args, **kwargs):
        self.clock[0] += 100
        return super().execute(func, *args, **kwargs)


def test_refresh_longer_than_the_cache_lifetime_is_stamped_at_completion(
    data_dir, monkeypatch
):
    clock = [1000.0]
    worker = SlowClockWorker(clock)
    monkeypatch.setattr(main, "SCRAPER_WORKER", worker)
    monkeypatch.setattr(main, "UPDATE_CACHE", {"timestamp": 0, "results": None})
    monkeypatch.setattr(main, "SCRAPER_ACTIVE", False)
    monkeypatch.setattr(main, "BUILD_STATUS", {"in_progress": False})
    monkeypatch.setattr(patcher, "get_patch_versions", lambda cfg: {})
    monkeypatch.setattr(main.time, "time", lambda: clock[0])

    main.check_updates()  # two lookups: 200 fake seconds, longer than the 60 s TTL
    calls = worker.calls
    main.check_updates()

    assert main.UPDATE_CACHE["timestamp"] == 1200.0
    assert worker.calls == calls


def test_unparsable_versions_do_not_break_the_refresh(updates_env):
    config_store.update_config(
        lambda c: c["apps"][0].update(last_successful_build={"app_version": "1.0"})
    )
    updates_env.release.set()
    updates_env.versions = ["not a version"]

    result = main.check_updates()

    assert result["com.a"]["latest"] == "not a version"
    assert result["com.a"]["has_update"] is True


# --- GET /api/ui/source_updates is gone -------------------------------------------


def test_source_updates_route_is_gone_and_touches_nothing(data_dir, monkeypatch):
    from fastapi.testclient import TestClient

    before = (data_dir / "config.yaml").read_bytes()
    monkeypatch.setattr(main, "update_config", lambda *a: pytest.fail("config write"))

    response = TestClient(main.app).get("/api/ui/source_updates")

    assert response.status_code == 404
    assert (data_dir / "config.yaml").read_bytes() == before
    for name in ("check_source_updates", "SOURCE_UPDATE_CACHE", "SOURCE_UPDATE_LOCK"):
        assert not hasattr(main, name)
