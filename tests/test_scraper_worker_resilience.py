"""Scraper worker recovery, shutdown, and the routes that report its state."""

import logging
import queue
import threading
import time
from collections.abc import Callable

import pytest

from src import main, patcher

WAIT = 5
SECRET = "lic-abc-123-secret"


class FakeBrowser:
    """Downloader stand-in; counts closes."""

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class FakeLauncher:
    """Launch factory that fails the first `failures` times."""

    def __init__(self, failures: int = 0, message: str = "no display") -> None:
        self.failures = failures
        self.message = message
        self.calls = 0
        self.browsers: list[FakeBrowser] = []

    def __call__(self) -> FakeBrowser:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError(self.message)
        browser = FakeBrowser()
        self.browsers.append(browser)
        return browser


class FakeClock:
    """Manually advanced clock; the worker never really sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def start_worker(launcher: FakeLauncher, clock: FakeClock) -> main.ScraperWorker:
    worker = main.ScraperWorker(launch=launcher, clock=clock, cooldown=60.0)
    worker.start()
    assert worker.initialized.wait(WAIT)
    return worker


def run_task(scraper: object) -> str:
    return "ran"


# --- outcome 1: recovery -------------------------------------------------------


def test_failed_launch_gives_unavailable_error_not_the_raw_exception():
    worker = start_worker(FakeLauncher(failures=99), FakeClock())

    with pytest.raises(main.ScraperUnavailableError, match="no display"):
        worker.execute(run_task)
    worker.shutdown()


def test_no_new_launch_attempt_during_the_cooldown():
    launcher = FakeLauncher(failures=99)
    clock = FakeClock()
    worker = start_worker(launcher, clock)

    clock.now += 59
    for _ in range(3):
        with pytest.raises(main.ScraperUnavailableError):
            worker.execute(run_task)

    assert launcher.calls == 1
    worker.shutdown()


def test_task_after_the_cooldown_relaunches_and_runs():
    launcher = FakeLauncher(failures=1)
    clock = FakeClock()
    worker = start_worker(launcher, clock)
    with pytest.raises(main.ScraperUnavailableError):
        worker.execute(run_task)

    clock.now += 61
    result = worker.execute(run_task)

    assert result == "ran"
    assert launcher.calls == 2
    assert worker.status()["state"] == "available"
    worker.shutdown()


def test_second_failure_starts_a_new_cooldown():
    launcher = FakeLauncher(failures=2)
    clock = FakeClock()
    worker = start_worker(launcher, clock)
    clock.now += 61
    with pytest.raises(main.ScraperUnavailableError):
        worker.execute(run_task)

    clock.now += 30
    with pytest.raises(main.ScraperUnavailableError):
        worker.execute(run_task)

    assert launcher.calls == 2
    assert worker.status()["retry_in"] == 30
    worker.shutdown()


def test_launch_error_text_is_redacted_and_logged_not_printed(
    monkeypatch, caplog, capsys
):
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", SECRET)
    launcher = FakeLauncher(failures=99, message=f"bad key {SECRET}")

    with caplog.at_level("ERROR", logger=main.logger.name):
        worker = start_worker(launcher, FakeClock())
        with pytest.raises(main.ScraperUnavailableError) as raised:
            worker.execute(run_task)
    worker.shutdown()

    assert SECRET not in str(raised.value)
    assert SECRET not in caplog.text
    assert "bad key" in caplog.text
    assert SECRET not in worker.status()["error"]
    assert "[Worker]" not in capsys.readouterr().out


# --- outcome 2: shutdown -------------------------------------------------------


def test_shutdown_closes_the_browser_and_stops_the_thread():
    launcher = FakeLauncher()
    worker = start_worker(launcher, FakeClock())

    worker.shutdown()
    worker.join(WAIT)

    assert not worker.is_alive()
    assert launcher.browsers[0].closed == 1
    with pytest.raises(main.ScraperUnavailableError):
        worker.execute(run_task)


# --- outcome 3: /api/ui/updates degrades ---------------------------------------


class DownWorker:
    """Worker stand-in that is unavailable for every task."""

    def execute(self, func, *args, **kwargs):
        raise main.ScraperUnavailableError("browser launch failed: no display")


class BrokenLookupWorker:
    def execute(self, func, *args, **kwargs):
        raise RuntimeError(f"lookup blew up {SECRET}")


@pytest.fixture
def updates_env(data_dir, monkeypatch):
    monkeypatch.setattr(main, "UPDATE_CACHE", {"timestamp": 0, "results": None})
    monkeypatch.setattr(main, "SCRAPER_ACTIVE", False)
    monkeypatch.setattr(main, "BUILD_STATUS", {"in_progress": False})
    monkeypatch.setattr(patcher, "get_patch_versions", lambda cfg: {})


def test_updates_with_scraper_down_answers_with_per_app_errors(
    updates_env, monkeypatch
):
    monkeypatch.setattr(main, "SCRAPER_WORKER", DownWorker())

    results = main.check_updates()

    assert set(results) == {"com.a", "com.b"}
    for entry in results.values():
        assert entry["status"] == "Error"
        assert "browser launch failed" in entry["error"]
        assert entry["latest"] == "Unknown"


def test_updates_with_scraper_down_still_serves_cached_versions(
    updates_env, monkeypatch
):
    monkeypatch.setattr(main, "SCRAPER_WORKER", DownWorker())
    patcher.save_version_cache(
        {"com.a": {"versions": ["9.9"], "timestamp": time.time() - 99999}}
    )

    results = main.check_updates()

    assert results["com.a"]["latest"] == "9.9"
    assert results["com.a"]["status"] != "Error"
    assert results["com.b"]["status"] == "Error"


def test_updates_with_a_failing_lookup_degrades_and_redacts(
    updates_env, monkeypatch, caplog
):
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", SECRET)
    monkeypatch.setattr(main, "SCRAPER_WORKER", BrokenLookupWorker())

    results = main.check_updates()

    assert results["com.a"]["status"] == "Error"
    assert "lookup blew up" in results["com.a"]["error"]
    assert SECRET not in results["com.a"]["error"]
    assert SECRET not in caplog.text


# --- outcome 4: tools status ---------------------------------------------------


def test_tools_status_reports_the_browser_state(monkeypatch):
    launcher = FakeLauncher(failures=99)
    worker = start_worker(launcher, FakeClock())
    monkeypatch.setattr(main, "SCRAPER_WORKER", worker)

    status = main.get_tools_status()
    worker.shutdown()

    assert status["browser"]["state"] == "unavailable"
    assert "no display" in status["browser"]["error"]
    assert status["browser"]["retry_in"] == 60


def test_tools_status_reports_available_browser(monkeypatch):
    worker = start_worker(FakeLauncher(), FakeClock())
    monkeypatch.setattr(main, "SCRAPER_WORKER", worker)

    status = main.get_tools_status()
    worker.shutdown()

    assert status["browser"] == {
        "state": "available",
        "error": None,
        "retry_in": None,
        "busy_since": None,
        "license_key_rejected": False,
    }


def test_worker_state_is_starting_before_the_first_launch_finishes():
    gate = threading.Event()

    def slow_launch() -> FakeBrowser:
        assert gate.wait(WAIT)
        return FakeBrowser()

    worker = main.ScraperWorker(launch=slow_launch, clock=FakeClock())
    worker.start()

    assert worker.status()["state"] == "starting"
    gate.set()
    assert worker.initialized.wait(WAIT)
    worker.shutdown()


# --- helpers for deadline-bounded calls ----------------------------------------


def call_with_deadline(func: Callable[[], object], seconds: float = WAIT):
    """Run `func` in a thread; return ("done", value|exception) or ("hung", None)."""
    box: list[object] = []

    def target() -> None:
        try:
            box.append(func())
        except BaseException as error:  # noqa: BLE001 - reported to the asserts
            box.append(error)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    return ("done", box[0]) if box else ("hung", None)


# --- item 3: no caller is left waiting -------------------------------------------


def test_execute_after_shutdown_fails_instead_of_hanging():
    worker = start_worker(FakeLauncher(), FakeClock())
    worker.shutdown()
    worker.join(WAIT)

    outcome, value = call_with_deadline(lambda: worker.execute(run_task))

    assert outcome == "done"
    assert isinstance(value, main.ScraperUnavailableError)


def test_task_queued_behind_stop_is_answered():
    worker = start_worker(FakeLauncher(), FakeClock())
    worker.shutdown()  # STOP is queued
    answer: queue.Queue = queue.Queue()
    worker.task_queue.put(main._Task(run_task, (), {}, answer))  # behind STOP

    worker.join(WAIT)

    _, error = answer.get(timeout=WAIT)
    assert isinstance(error, main.ScraperUnavailableError)


def test_shutdown_requested_while_a_task_is_being_queued_waits_for_the_queueing():
    worker = start_worker(FakeLauncher(), FakeClock())
    real_put = worker.task_queue.put
    stopper = threading.Thread(target=worker.shutdown)
    stopper_blocked: list[bool] = []

    def put_while_shutdown_is_requested(item: object) -> None:
        # Runs inside execute's critical section: shutdown needs the same lock,
        # so it must still be waiting after a generous pause.
        if item is not None and not stopper_blocked:
            stopper.start()
            stopper.join(0.2)
            stopper_blocked.append(stopper.is_alive())
        real_put(item)  # type: ignore[arg-type]

    setattr(worker.task_queue, "put", put_while_shutdown_is_requested)

    outcome, value = call_with_deadline(lambda: worker.execute(run_task))
    stopper.join(WAIT)
    worker.join(WAIT)

    assert stopper_blocked == [True]
    assert (outcome, value) == ("done", "ran")  # queued before STOP, so it runs
    assert not stopper.is_alive() and not worker.is_alive()


def test_callers_queued_behind_a_running_task_are_all_answered_by_shutdown():
    worker = start_worker(FakeLauncher(), FakeClock())
    entered, release = threading.Event(), threading.Event()

    def long_task(scraper: object) -> str:
        entered.set()
        assert release.wait(WAIT)
        return "done"

    running, running_results = _start(worker, long_task)
    assert entered.wait(WAIT)
    waiting = [call_in_thread(worker) for _ in range(3)]
    deadline = time.monotonic() + WAIT
    while worker.task_queue.qsize() < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert worker.task_queue.qsize() == 3

    worker.shutdown()  # STOP queues behind the three callers' tasks
    release.set()
    running.join(WAIT)
    for thread, _ in waiting:
        thread.join(WAIT)
    worker.join(WAIT)

    assert running_results == ["done"]
    assert [results for _, results in waiting] == [["ran"]] * 3
    assert not worker.is_alive()


def call_in_thread(worker: main.ScraperWorker) -> tuple[threading.Thread, list[object]]:
    """Start `worker.execute(run_task)` in a thread; the list gets its result."""
    return _start(worker, run_task)


def test_task_raising_system_exit_answers_the_caller_and_keeps_the_worker():
    worker = start_worker(FakeLauncher(), FakeClock())

    def exits(scraper: object) -> None:
        raise SystemExit(3)

    outcome, value = call_with_deadline(lambda: worker.execute(exits))
    after = call_with_deadline(lambda: worker.execute(run_task))

    assert outcome == "done"
    assert isinstance(value, RuntimeError) and "SystemExit" in str(value)
    assert after == ("done", "ran")
    worker.shutdown()


def test_worker_thread_dying_unexpectedly_does_not_strand_callers(monkeypatch):
    crashes: list[BaseException | None] = []
    # Taking over the hook records the deliberate crash; pytest's own hook would
    # report it as an unhandled thread exception and hide a real one in noise.
    monkeypatch.setattr(
        threading, "excepthook", lambda args: crashes.append(args.exc_value)
    )
    worker = start_worker(FakeLauncher(), FakeClock())
    worker.task_queue.put("not a task")  # makes the loop itself fail

    outcome, value = call_with_deadline(lambda: worker.execute(run_task))
    worker.join(WAIT)

    assert outcome == "done"
    assert isinstance(value, main.ScraperUnavailableError)
    assert len(crashes) == 1 and isinstance(crashes[0], AttributeError)


# --- item 4: shutdown order, safety, truthful state --------------------------------


class SpyWorker:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def shutdown(self) -> None:
        self.calls.append("worker shutdown")

    def join(self, timeout: float | None = None) -> None:
        self.calls.append("join")

    def is_alive(self) -> bool:
        return True


class SpyScheduler:
    def __init__(self, calls: list[str], error: Exception | None = None) -> None:
        self.calls = calls
        self.error = error
        self.running = True

    def shutdown(self, wait: bool = True) -> None:
        self.calls.append(f"scheduler shutdown wait={wait}")
        if self.error:
            raise self.error


def test_shutdown_event_stops_the_worker_first_and_does_not_wait_for_scheduler_jobs(
    monkeypatch,
):
    calls: list[str] = []
    monkeypatch.setattr(main, "SCRAPER_WORKER", SpyWorker(calls))
    monkeypatch.setattr(main, "scheduler", SpyScheduler(calls))

    main.shutdown_event()

    assert calls == ["worker shutdown", "scheduler shutdown wait=False", "join"]


def test_shutdown_event_survives_a_scheduler_error(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(main, "SCRAPER_WORKER", SpyWorker(calls))
    monkeypatch.setattr(
        main, "scheduler", SpyScheduler(calls, RuntimeError("Scheduler is not running"))
    )

    main.shutdown_event()

    assert "worker shutdown" in calls and "join" in calls


def test_shutdown_event_with_a_worker_that_never_started(monkeypatch):
    monkeypatch.setattr(
        main, "SCRAPER_WORKER", main.ScraperWorker(launch=FakeLauncher())
    )
    monkeypatch.setattr(main, "scheduler", SpyScheduler([]))

    main.shutdown_event()  # must not raise (join on an unstarted thread would)


def test_state_is_stopping_while_a_task_runs_and_stopped_after():
    worker = start_worker(FakeLauncher(), FakeClock())
    entered, release = threading.Event(), threading.Event()

    def long_task(scraper: object) -> str:
        entered.set()
        assert release.wait(WAIT)
        return "done"

    caller, results = _start(worker, long_task)
    assert entered.wait(WAIT)

    worker.shutdown()
    during = worker.status()["state"]
    release.set()
    caller.join(WAIT)
    worker.join(WAIT)

    assert during == "stopping"
    assert results == ["done"]
    assert worker.status()["state"] == "stopped"


def _start(
    worker: main.ScraperWorker, func: Callable[..., object]
) -> tuple[threading.Thread, list[object]]:
    results: list[object] = []
    thread = threading.Thread(target=lambda: results.append(worker.execute(func)))
    thread.start()
    return thread, results


# --- item 5: a build with the browser down says so --------------------------------


def test_build_with_browser_down_sets_a_redacted_failure_and_logs_it(
    monkeypatch, caplog
):
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", SECRET)

    class SecretDown:
        def execute(self, func, *args, **kwargs):
            raise main.ScraperUnavailableError(f"launch failed {SECRET}")

    monkeypatch.setattr(main, "SCRAPER_WORKER", SecretDown())
    monkeypatch.setattr(main, "BUILD_STATUS", {"in_progress": False, "message": "Idle"})
    monkeypatch.setattr(main, "SCRAPER_ACTIVE", False)

    with caplog.at_level(logging.ERROR, logger=main.logger.name):
        main.build_task(package_ids=[], is_cron=False)

    assert main.BUILD_STATUS["message"].startswith("Failed: ")
    assert "launch failed" in main.BUILD_STATUS["message"]
    assert SECRET not in main.BUILD_STATUS["message"]
    assert "launch failed" in caplog.text and SECRET not in caplog.text
    assert main.BUILD_STATUS["in_progress"] is False


# --- item 2: browser down and a patch list unreadable -----------------------------


def test_updates_with_browser_down_and_unreadable_patch_list_degrade(
    updates_env, monkeypatch
):
    json_path = patcher.get_source_paths("own/src")[".json"]
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text("{truncated")
    monkeypatch.setattr(main, "SCRAPER_WORKER", DownWorker())

    results = main.check_updates()

    assert set(results) == {"com.a", "com.b"}
    assert "own/src" in results["com.a"]["error"]
    assert results["com.a"]["status"] == "Error"


# --- item 6: a real lookup failure reaches the UI ----------------------------------


class FailingScraper:
    def latest_version(self, package_name: str) -> str:
        raise RuntimeError(f"blocked by captcha {SECRET}")


class ScraperInlineWorker:
    def __init__(self, scraper: object) -> None:
        self.scraper = scraper

    def execute(self, func, *args, **kwargs):
        return func(*args, scraper=self.scraper, **kwargs)


def test_get_supported_versions_raises_lookup_errors_only_when_asked(updates_env):
    config = main.load_config()
    patcher.save_version_cache(
        {"com.a": {"versions": ["9.9"], "timestamp": time.time() - 99999}}
    )

    quiet = patcher.get_supported_versions(
        "com.a", config, scraper=FailingScraper(), quiet=True
    )
    with pytest.raises(patcher.VersionLookupError, match="blocked by captcha"):
        patcher.get_supported_versions(
            "com.a",
            config,
            scraper=FailingScraper(),
            quiet=True,
            raise_lookup_errors=True,
        )

    assert quiet == (["9.9"], None, None)


def test_updates_shows_a_lookup_failure_and_keeps_cached_versions(
    updates_env, monkeypatch
):
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", SECRET)
    monkeypatch.setattr(main, "SCRAPER_WORKER", ScraperInlineWorker(FailingScraper()))
    patcher.save_version_cache(
        {"com.a": {"versions": ["9.9"], "timestamp": time.time() - 99999}}
    )

    results = main.check_updates()

    assert results["com.a"]["latest"] == "9.9"
    assert results["com.b"]["status"] == "Error"
    assert "blocked by captcha" in results["com.b"]["error"]
    assert SECRET not in results["com.b"]["error"]


# --- item 7: surviving mutants -------------------------------------------------------


def test_error_is_cleared_after_a_successful_relaunch():
    clock = FakeClock()
    worker = start_worker(FakeLauncher(failures=1), clock)
    clock.now += 61
    worker.execute(run_task)

    assert worker.status() == {
        "state": "available",
        "error": None,
        "retry_in": None,
        "busy_since": None,
        "license_key_rejected": False,
    }
    worker.shutdown()


def test_failing_close_is_logged_and_does_not_stop_the_shutdown(caplog):
    launcher = FakeLauncher()
    worker = start_worker(launcher, FakeClock())

    def bad_close() -> None:
        raise OSError("pipe closed")

    launcher.browsers[0].close = bad_close  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR, logger=main.logger.name):
        worker.shutdown()
        worker.join(WAIT)

    assert not worker.is_alive()
    assert "Closing the browser failed" in caplog.text and "pipe closed" in caplog.text


# --- item 8: only "available" reads as healthy ---------------------------------------


def test_scraper_pill_is_green_only_for_the_available_state():
    html = (main.PROJECT_ROOT / "src" / "templates" / "index.html").read_text()

    assert "browser.state === 'available'" in html
    assert "browser.state !== 'unavailable'" not in html
