"""Build flag, activity flag, update cache, sync single-flight, worker status."""

import logging
import threading
import time
from collections.abc import Callable

import pytest
from fastapi import BackgroundTasks, HTTPException

from src import main, patcher
from tests.test_scraper_worker_resilience import (
    WAIT,
    FakeClock,
    FakeLauncher,
    start_worker,
)


class RecordingScheduler:
    """Scheduler stand-in: records jobs and refuses a repeated id like APScheduler."""

    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def add_job(self, func: Callable, **kwargs: object) -> None:
        if any(job["id"] == kwargs["id"] for job in self.jobs):
            raise RuntimeError(f"Job identifier ({kwargs['id']}) conflicts")
        self.jobs.append({"func": func, **kwargs})

    def get_jobs(self) -> list:
        return []


@pytest.fixture
def state(data_dir, monkeypatch) -> RecordingScheduler:
    """Fresh module state, a recording scheduler and silent notifications."""
    scheduler = RecordingScheduler()
    monkeypatch.setattr(main, "scheduler", scheduler)
    monkeypatch.setattr(main, "UPDATE_CACHE", {"timestamp": 0, "results": None})
    monkeypatch.setattr(main, "SCRAPER_ACTIVE", False)
    monkeypatch.setattr(main, "BUILD_STATUS", {"in_progress": False, "message": "Idle"})
    monkeypatch.setattr(main, "TOOLS_UPDATING", False)
    monkeypatch.setattr(main, "send_notification", lambda *a, **k: None)
    monkeypatch.setattr(patcher, "get_patch_versions", lambda cfg: {})
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)
    return scheduler


def in_thread(func: Callable[[], object]) -> threading.Thread:
    thread = threading.Thread(target=func, daemon=True)
    thread.start()
    return thread


def join(thread: threading.Thread) -> None:
    thread.join(WAIT)
    assert not thread.is_alive()


def trigger() -> dict:
    return main.trigger_build(main.TriggerBuildPayload(package_ids=None))


class Gate:
    """A function that parks until released and reports that it was entered."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args: object, **kwargs: object) -> list[str]:
        self.entered.set()
        assert self.release.wait(WAIT)
        return []


# --- item 1: one build at a time, flags owned by their activity ------------


def test_a_second_trigger_while_the_first_is_queued_is_refused(state):
    first = trigger()
    second = trigger()

    assert first == {"status": "Build started"}
    assert second == {"status": "Build already in progress"}
    assert len(state.jobs) == 1
    assert main.BUILD_STATUS["in_progress"] is True


def test_cron_build_is_refused_while_a_manual_build_is_queued(state):
    trigger()

    main.build_task(is_cron=True)

    assert len(state.jobs) == 1
    assert main.BUILD_STATUS["in_progress"] is True


def test_two_triggers_in_the_same_second_get_distinct_job_ids(state, monkeypatch):
    monkeypatch.setattr(main.time, "time", lambda: 1000.0)
    trigger()
    main.BUILD_STATUS["in_progress"] = False  # the first build has ended

    assert trigger() == {"status": "Build started"}
    assert len({job["id"] for job in state.jobs}) == 2


def test_a_job_that_cannot_be_scheduled_does_not_leave_the_build_flag_raised(
    state, monkeypatch
):
    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("scheduler is down")

    monkeypatch.setattr(state, "add_job", refuse)

    with pytest.raises(RuntimeError):
        trigger()

    assert main.BUILD_STATUS["in_progress"] is False


def test_refresh_ending_does_not_clear_the_flags_of_a_build_behind_it(
    state, monkeypatch
):
    worker = start_worker(FakeLauncher(), FakeClock())
    monkeypatch.setattr(main, "SCRAPER_WORKER", worker)
    refresh, pipeline = Gate(), Gate()
    monkeypatch.setattr(main, "get_supported_versions", refresh)
    monkeypatch.setattr(patcher, "run_pipeline", pipeline)
    refresh_thread = in_thread(main.refresh_versions_task)
    assert refresh.entered.wait(WAIT)

    build_thread = in_thread(lambda: main.build_task(package_ids=[], is_cron=False))
    deadline = time.monotonic() + WAIT
    while not main.BUILD_STATUS["in_progress"] and time.monotonic() < deadline:
        time.sleep(0.01)
    queued_flag = main.BUILD_STATUS["in_progress"]
    refresh.release.set()
    join(refresh_thread)
    assert pipeline.entered.wait(WAIT)
    during_build = (main.SCRAPER_ACTIVE, main.BUILD_STATUS["in_progress"])
    pipeline.release.set()
    join(build_thread)
    worker.shutdown()

    assert queued_flag is True
    assert during_build == (True, True)
    assert (main.SCRAPER_ACTIVE, main.BUILD_STATUS["in_progress"]) == (False, False)


def test_refresh_is_skipped_while_a_build_is_queued_or_running(state, monkeypatch):
    class ForbiddenWorker:
        def execute(self, *args: object, **kwargs: object) -> None:
            pytest.fail("a refresh was queued behind the build")

    monkeypatch.setattr(main, "SCRAPER_WORKER", ForbiddenWorker())
    trigger()

    main.refresh_versions_task()

    assert main.SCRAPER_ACTIVE is False


def test_updates_poll_does_not_block_while_a_build_runs_through_the_real_worker(
    state, monkeypatch
):
    worker = start_worker(FakeLauncher(), FakeClock())
    monkeypatch.setattr(main, "SCRAPER_WORKER", worker)
    pipeline = Gate()
    monkeypatch.setattr(patcher, "run_pipeline", pipeline)
    build_thread = in_thread(lambda: main.build_task(package_ids=[], is_cron=False))
    assert pipeline.entered.wait(WAIT)

    poll = in_thread(main.check_updates)
    join(poll)
    pipeline.release.set()
    join(build_thread)
    worker.shutdown()

    assert main.UPDATE_CACHE["timestamp"] == 0  # the build invalidated the cache


# --- item 2: a refresh that began before an invalidation is not fresh ----------------


class ParkedWorker:
    """Worker stand-in whose lookups park until released; counts lookups."""

    def __init__(self) -> None:
        self.gate = Gate()
        self.calls = 0

    def execute(self, func, *args, **kwargs):
        self.calls += 1
        self.gate()
        return (["1.0"], None, None)


def test_refresh_that_started_before_an_invalidation_is_not_served_as_fresh(
    state, monkeypatch
):
    worker = ParkedWorker()
    monkeypatch.setattr(main, "SCRAPER_WORKER", worker)
    refresh = in_thread(main.check_updates)
    assert worker.gate.entered.wait(WAIT)

    trigger()  # invalidates the cache while the refresh is running
    main.BUILD_STATUS["in_progress"] = False  # and the build is already done
    worker.gate.release.set()
    join(refresh)
    calls_after_first_refresh = worker.calls
    main.check_updates()

    assert main.UPDATE_CACHE["results"] is not None
    assert worker.calls > calls_after_first_refresh


def test_refresh_without_an_invalidation_is_still_stamped_fresh(state, monkeypatch):
    worker = ParkedWorker()
    worker.gate.release.set()
    monkeypatch.setattr(main, "SCRAPER_WORKER", worker)
    monkeypatch.setattr(main.time, "time", lambda: 5000.0)

    main.check_updates()

    assert main.UPDATE_CACHE["timestamp"] == 5000.0


# --- item 3: one sync at a time -------------------------------------------------------


def test_second_forced_sync_request_while_one_is_queued_starts_nothing(
    state, monkeypatch
):
    first, second = BackgroundTasks(), BackgroundTasks()

    one = main.sync_discovery(first)
    two = main.sync_discovery(second)

    assert len(first.tasks) == 1
    assert second.tasks == []
    assert "sync" not in one
    assert two["sync"] == "already running"
    assert "apps" in two


def test_a_new_sync_request_is_accepted_once_the_last_one_has_run(state, monkeypatch):
    monkeypatch.setattr(main, "update_tools", lambda *a, **k: False)
    first = BackgroundTasks()
    main.sync_discovery(first)
    for task in first.tasks:
        task.func(*task.args, **task.kwargs)
    second = BackgroundTasks()

    answer = main.sync_discovery(second)

    assert "sync" not in answer
    assert len(second.tasks) == 1


def test_sync_request_is_refused_while_a_build_is_syncing(state):
    with patcher.SYNC_LOCK:
        tasks = BackgroundTasks()

        answer = main.sync_discovery(tasks)

    assert answer["sync"] == "already running"
    assert tasks.tasks == []


SYNC_CONFIG = {
    "repositories": {"cli": "own/cli"},
    "tools": {"cli_jar": "bin/cli.jar"},
    "sources": [],
}


def test_two_update_tools_calls_never_overlap(data_dir, monkeypatch):
    active, peak = [0], [0]
    counter = threading.Lock()
    gate = Gate()

    def slow_check(repo, paths, force=False) -> bool:
        with counter:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        gate.entered.set()
        gate.release.wait(WAIT)
        with counter:
            active[0] -= 1
        return False

    monkeypatch.setattr(patcher, "check_and_download_release", slow_check)
    threads = [
        in_thread(lambda: patcher.update_tools(dict(SYNC_CONFIG), quiet=True))
        for _ in range(2)
    ]
    assert gate.entered.wait(WAIT)
    time.sleep(0.2)  # a second sync, if allowed, would be inside by now
    gate.release.set()
    for thread in threads:
        join(thread)

    assert peak[0] == 1


def test_add_source_is_refused_while_a_sync_runs(state, monkeypatch):
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("a second sync was started")

    monkeypatch.setattr(main, "check_and_download_release", forbidden)

    with patcher.SYNC_LOCK, pytest.raises(HTTPException) as refused:
        main.add_source(main.AddSourcePayload(repo="new/repo"))

    assert refused.value.status_code == 409
    assert "sync" in refused.value.detail.lower()


def test_add_source_releases_the_sync_lock_when_its_sync_fails(state, monkeypatch):
    def broken(*args: object, **kwargs: object) -> None:
        raise main.GitHubRateLimitError(None)

    monkeypatch.setattr(main, "check_and_download_release", broken)

    with pytest.raises(HTTPException) as failed:
        main.add_source(main.AddSourcePayload(repo="new/repo"))

    assert failed.value.status_code == 429
    assert patcher.SYNC_LOCK.acquire(blocking=False)
    patcher.SYNC_LOCK.release()


# --- item 6: busy since, and starting during a relaunch ---------------------


def test_status_reports_since_when_a_task_runs():
    worker = start_worker(FakeLauncher(), FakeClock())
    task = Gate()
    idle = worker.status()["busy_since"]

    thread = in_thread(lambda: worker.execute(lambda scraper: task()))
    assert task.entered.wait(WAIT)
    during = worker.status()["busy_since"]
    task.release.set()
    join(thread)
    after = worker.status()["busy_since"]
    worker.shutdown()

    assert idle is None and after is None
    assert isinstance(during, float) and abs(during - time.time()) < 60


def test_busy_since_is_cleared_when_a_task_raises():
    worker = start_worker(FakeLauncher(), FakeClock())

    def boom(scraper: object) -> None:
        raise ValueError("task failed")

    with pytest.raises(ValueError):
        worker.execute(boom)
    busy_since = worker.status()["busy_since"]
    worker.shutdown()

    assert busy_since is None


def test_state_is_starting_while_a_task_relaunches_the_browser():
    gate = Gate()

    class SlowRelaunch(FakeLauncher):
        def __call__(self):
            if self.calls >= 1:
                self.calls += 1
                gate.entered.set()
                assert gate.release.wait(WAIT)
                self.browsers.append(object())  # type: ignore[arg-type]
                return self.browsers[-1]
            return super().__call__()

    launcher = SlowRelaunch(failures=1)
    clock = FakeClock()
    worker = start_worker(launcher, clock)
    clock.now += 61
    thread = in_thread(lambda: worker.execute(lambda scraper: "ran"))
    assert gate.entered.wait(WAIT)

    during = worker.status()
    gate.release.set()
    join(thread)
    worker.shutdown()

    assert during["state"] == "starting"
    assert during["retry_in"] is None


# --- item 7: a failing close never stops the drain --------------------------


def test_any_exception_from_close_is_logged_and_the_queue_is_still_answered(caplog):
    launcher = FakeLauncher()
    worker = start_worker(launcher, FakeClock())

    def bad_close() -> None:
        raise ValueError("close exploded")

    launcher.browsers[0].close = bad_close  # type: ignore[method-assign]
    worker.shutdown()
    answer: main.queue.Queue = main.queue.Queue()
    worker.task_queue.put(main._Task(lambda scraper: "ran", (), {}, answer))
    with caplog.at_level(logging.ERROR, logger=main.logger.name):
        worker.join(WAIT)

    _, error = answer.get(timeout=WAIT)
    assert isinstance(error, main.ScraperUnavailableError)
    assert not worker.is_alive()
    assert "close exploded" in caplog.text
