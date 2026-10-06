"""Follow-up round: stuck claims, guards, logging, downloads and release sync."""

import asyncio
import io
import json
import logging
import os
import random
import string
import sys
import threading
import time

import pytest
import requests
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from requests.adapters import HTTPAdapter

from src import main, patcher, request_guards, secrets_store
from tests.test_release_sync_review import (
    GOOD_LIST,
    OLD_LIST,
    RAW_ROOT,
    REPO,
    Net,
    asset_url,
    build_release,
)
from tests.test_worker_bookkeeping import RecordingScheduler, join, trigger

CHUNK = 64 * 1024


@pytest.fixture
def state(data_dir, monkeypatch) -> RecordingScheduler:
    scheduler = RecordingScheduler()
    monkeypatch.setattr(main, "scheduler", scheduler)
    monkeypatch.setattr(main, "UPDATE_CACHE", {"timestamp": 0, "results": None})
    monkeypatch.setattr(main, "SCRAPER_ACTIVE", False)
    monkeypatch.setattr(main, "BUILD_STATUS", {"in_progress": False, "message": "Idle"})
    return scheduler


@pytest.fixture
def paths(data_dir, monkeypatch):
    monkeypatch.setattr(patcher, "VERSIONS_FILE", data_dir / "bin" / "versions.json")
    source_paths = patcher.get_source_paths(REPO)
    source_paths[".mpp"].parent.mkdir(parents=True, exist_ok=True)
    return source_paths


# --- 1: no failure after a sync claim can leave it set ---


def write_list(content: object) -> None:
    path = patcher.get_source_paths(REPO)[".json"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content))


APPS_CONFIG = {"sources": [{"name": "S", "repo": REPO, "active": True}]}


@pytest.mark.parametrize(
    "content",
    [
        [{"name": "P", "compatiblePackages": {"com.a": []}}],
        {
            "appNames": ["not", "a", "map"],
            "patches": [{"compatiblePackages": {"com.a": []}}],
        },
        {"appNames": {"com.a": 5}, "patches": [{"compatiblePackages": {"com.a": []}}]},
        {"patches": "not a list"},
        [{"compatiblePackages": [{"packageName": "com.a", "name": 7}]}],
    ],
)
def test_app_discovery_survives_odd_list_shapes(data_dir, content):
    write_list(content)

    apps = patcher.get_all_available_apps(APPS_CONFIG)

    assert all(isinstance(app["name"], str) for app in apps)


def test_a_failure_after_the_sync_claim_does_not_leave_it_set(state, monkeypatch):
    def broken(config: dict) -> list:
        raise RuntimeError("discovery blew up")

    monkeypatch.setattr(main, "get_all_available_apps", broken)

    with pytest.raises(RuntimeError):
        main.sync_discovery(BackgroundTasks())

    assert main.TOOLS_UPDATING is False
    monkeypatch.setattr(main, "get_all_available_apps", lambda config: [])
    tasks = BackgroundTasks()
    main.sync_discovery(tasks)
    assert len(tasks.tasks) == 1


# --- 2: a claimed build runs or is released; cron touches only its own jobs ---


def test_manual_build_job_is_never_treated_as_missed(state):
    trigger()

    assert state.jobs[0]["misfire_grace_time"] is None


def test_everything_after_the_claim_is_under_one_try(state, monkeypatch):
    def broken() -> None:
        raise RuntimeError("cache invalidation failed")

    monkeypatch.setattr(main, "_invalidate_updates", broken)

    with pytest.raises(RuntimeError):
        trigger()

    assert main.BUILD_STATUS["in_progress"] is False


CRON_CONFIG = {"settings": {"cron_schedule": "0 3 * * *"}}


def real_scheduler(monkeypatch) -> main.BackgroundScheduler:
    scheduler = main.BackgroundScheduler()
    monkeypatch.setattr(main, "scheduler", scheduler)
    return scheduler


def noop() -> None:
    return None


def test_cron_setup_leaves_jobs_it_does_not_own(data_dir, monkeypatch):
    scheduler = real_scheduler(monkeypatch)
    scheduler.add_job(noop, id="manual_build_abc")
    scheduler.add_job(noop, id="initial_version_refresh")
    scheduler.add_job(noop, id="periodic_build")

    main.setup_cron_job(CRON_CONFIG)

    ids = {job.id for job in scheduler.get_jobs()}
    assert {"manual_build_abc", "initial_version_refresh"} <= ids
    assert {"periodic_build", "version_refresh"} <= ids


def test_cron_setup_without_a_schedule_removes_only_its_own_jobs(data_dir, monkeypatch):
    scheduler = real_scheduler(monkeypatch)
    scheduler.add_job(noop, id="manual_build_abc")
    scheduler.add_job(noop, id="periodic_build")
    scheduler.add_job(noop, id="version_refresh")

    main.setup_cron_job({"settings": {"cron_schedule": ""}})

    assert {job.id for job in scheduler.get_jobs()} == {"manual_build_abc"}


def test_cron_setup_survives_a_job_that_vanishes(data_dir, monkeypatch):
    scheduler = real_scheduler(monkeypatch)
    scheduler.add_job(noop, id="periodic_build")

    def vanished(job_id: str, *args: object, **kwargs: object) -> None:
        raise main.JobLookupError(job_id)

    monkeypatch.setattr(scheduler, "remove_job", vanished)

    main.setup_cron_job(CRON_CONFIG)  # must not raise


class SlowStatus(dict):
    """Build status whose read of the flag lingers, to expose a missing lock."""

    def __init__(self, barrier: threading.Barrier) -> None:
        super().__init__(in_progress=False, message="Idle")
        self.barrier = barrier

    def __getitem__(self, key: str) -> object:
        value = super().__getitem__(key)
        if key == "in_progress":
            try:
                self.barrier.wait(0.3)
            except threading.BrokenBarrierError:
                pass
        return value


def test_claiming_the_build_slot_is_atomic(state, monkeypatch):
    monkeypatch.setattr(main, "BUILD_STATUS", SlowStatus(threading.Barrier(2)))
    results: list[bool] = []

    def claim() -> None:
        results.append(main._claim_build())

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        join(thread)

    assert sorted(results) == [False, True]


def test_a_skipped_cron_tick_is_logged_as_a_warning(state, caplog):
    main._claim_build()

    with caplog.at_level(logging.WARNING, logger=main.logger.name):
        main.build_task(is_cron=True)

    assert any(
        r.levelno == logging.WARNING and "not started" in r.message
        for r in caplog.records
    )


# --- 4, 7: ALLOWED_HOSTS "set" means a usable entry; one server URL reader ---


@pytest.mark.parametrize("allowed", [",", " , ,", "bad host", "c@d", ",,,"])
def test_unusable_allowed_hosts_entries_do_not_drop_the_server_url(allowed):
    names = request_guards.allowed_host_names("https://proxy.example", allowed)

    assert names == frozenset({"proxy.example"})
    assert request_guards.is_origin_allowed(
        "https://proxy.example", "10.0.0.2:8000", "https://proxy.example", allowed
    )


def test_one_usable_entry_among_junk_still_drops_the_server_url():
    names = request_guards.allowed_host_names(
        "https://proxy.example", ",junk x,https://nas.lan"
    )

    assert names == frozenset({"nas.lan"})


@pytest.mark.parametrize(
    "url", ["ftp://name.example", "//name.example", "name.example"]
)
def test_a_server_url_without_http_scheme_allows_no_host(url):
    assert request_guards.allowed_host_names(url, None) == frozenset()


def test_port_zero_is_not_treated_as_the_default_port():
    assert not request_guards.is_origin_allowed("http://localhost", "localhost:0", None)
    assert request_guards.is_origin_allowed("http://localhost:0", "localhost:0", None)


# --- 5: an unchanged server URL never blocks a save ---

SETTINGS = {"cron_schedule": "0 4 * * *", "notification_urls": []}


@pytest.fixture
def client(data_dir):
    (data_dir / "apks" / "icons").mkdir(parents=True)
    return TestClient(main.app)


@pytest.mark.parametrize(
    "stored", ["", "localhost", "ftp://old.example", "http://x/a b"]
)
def test_an_unchanged_stored_server_url_does_not_block_a_save(client, stored):
    main.update_config(lambda c: c["settings"].update(server_url=stored))

    response = client.post(
        "/api/ui/save_settings", json={**SETTINGS, "server_url": stored}
    )

    assert response.status_code == 200
    saved = main.load_config()["settings"]
    assert saved["cron_schedule"] == "0 4 * * *"
    assert saved["server_url"] == stored.rstrip("/")


def test_a_changed_server_url_is_still_validated(client):
    main.update_config(lambda c: c["settings"].update(server_url=""))

    response = client.post(
        "/api/ui/save_settings", json={**SETTINGS, "server_url": "ftp://new.example"}
    )

    assert response.status_code == 400


# --- 6: the /api/ui/ rule under a root path ---


@pytest.mark.parametrize(
    ("path", "root_path", "refused"),
    [
        ("/x/api/ui/updates", "/x", True),
        ("/api/ui/updates", "/x", True),
        ("/api/ui/updates", "", True),
        ("/x/view/com.a", "/x", False),
        ("/x/download/a.apk", "/x", False),
        ("/x/api/apps", "/x", False),
        ("/xapi/ui/updates", "/x", False),
    ],
)
def test_ui_api_rule_holds_with_and_without_the_root_path_in_the_path(
    path, root_path, refused
):
    result = request_guards.is_fetch_site_refused(
        ["cross-site"], "GET", path, root_path
    )

    assert result is refused


def run_guard(path: str, root_path: str, headers: list[tuple[str, str]]) -> int:
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "root_path": root_path,
        "headers": [(k.encode(), v.encode()) for k, v in headers],
    }
    guard = request_guards.RequestGuardMiddleware(inner)
    asyncio.run(guard(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def test_middleware_refuses_cross_site_ui_api_under_uvicorn_root_path(data_dir):
    headers = [("host", "localhost"), ("sec-fetch-site", "cross-site")]

    assert run_guard("/x/api/ui/updates", "/x", headers) == 403
    assert run_guard("/x/view/a", "/x", headers) == 200


# --- 8: redact reads both lists longest first ---


def test_a_pending_value_that_contains_a_known_one_is_masked_whole(monkeypatch):
    monkeypatch.setattr(secrets_store, "_known", ("shortvalue",))
    monkeypatch.setattr(secrets_store, "_pending", ("shortvalue-and-more-text",))

    assert secrets_store.redact("x shortvalue-and-more-text y") == "x *** y"


# --- 9: request input never reaches stdout or the build log ---

SECRET = "stored-secret-value-0042"


def same_length_random(text: str) -> str:
    rng = random.Random(11)
    return "".join(rng.choice(string.ascii_lowercase) for _ in text)


def log_for(client: TestClient, monkeypatch, path: str) -> str:
    log = io.StringIO()
    monkeypatch.setattr(sys, "stdout", main.Tee(io.StringIO(), log))
    client.get(path)
    return log.getvalue()


@pytest.mark.parametrize(
    "template", ["/download/{}", "/view/{}", "/api/apps/{}", "/download/{}.apk"]
)
def test_request_input_is_not_written_to_the_build_log(client, monkeypatch, template):
    secrets_store.set_secret(secrets_store.CLOAKBROWSER_LICENSE_KEY, SECRET)

    with_secret = log_for(client, monkeypatch, template.format(SECRET))
    with_random = log_for(
        client, monkeypatch, template.format(same_length_random(SECRET))
    )

    assert with_secret == with_random
    assert SECRET not in with_secret


def test_a_get_cannot_write_arbitrary_lines_into_the_log(client, monkeypatch):
    text = log_for(client, monkeypatch, "/download/a%0A[FATAL]%20forged")

    assert "forged" not in text


# --- 10: no GitHub request uses ~/.netrc on any hop ---

ASSET = "https://github.com/own/src/releases/download/v1/p.mpp"
CDN = "https://release-assets.githubusercontent.com/x/p.mpp"
OTHER = "https://other.example/p.mpp"


class HopTransport:
    """Answers by URL and records the headers of every request."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.seen: list[tuple[str, dict[str, str]]] = []

    def send(self, request, **kwargs):
        self.seen.append((request.url, dict(request.headers)))
        status, headers, body = self.routes.get(request.url, (404, {}, b""))
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response._content = body
        response._content_consumed = True
        response.url = request.url
        response.request = request
        return response


@pytest.fixture
def hops(monkeypatch, tmp_path) -> HopTransport:
    transport = HopTransport()
    monkeypatch.setattr(
        HTTPAdapter, "send", lambda self, request, **kw: transport.send(request, **kw)
    )
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy",
                 "https_proxy", "all_proxy", "no_proxy", "REQUESTS_CA_BUNDLE",
                 "CURL_CA_BUNDLE"):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    netrc = tmp_path / "netrc"
    netrc.write_text(
        "".join(
            f"machine {host} login netrcuser password netrcpass\n"
            for host in (
                "github.com",
                "api.github.com",
                "raw.githubusercontent.com",
                "release-assets.githubusercontent.com",
                "other.example",
            )
        )
    )
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    return transport


def no_authorization(transport: HopTransport) -> bool:
    return all(
        "Authorization" not in {k.title() for k in headers}
        for _, headers in transport.seen
    )


@pytest.mark.parametrize("second_hop", [ASSET + "?again=1", CDN, OTHER])
def test_asset_download_redirects_never_use_netrc(hops, tmp_path, second_hop):
    hops.routes[ASSET] = (302, {"Location": second_hop}, b"")
    hops.routes[second_hop] = (200, {}, b"mpp")

    patcher.download_with_progress(ASSET, tmp_path / "p.mpp")

    assert (tmp_path / "p.mpp").read_bytes() == b"mpp"
    assert len(hops.seen) == 2
    assert no_authorization(hops)


def test_bounded_download_redirects_never_use_netrc(hops, tmp_path):
    hops.routes[ASSET] = (302, {"Location": CDN}, b"")
    hops.routes[CDN] = (200, {}, b"[]")

    patcher._download_bounded(ASSET, tmp_path / "p.json", 1024)

    assert len(hops.seen) == 2
    assert no_authorization(hops)


def test_unauthenticated_api_call_never_uses_netrc(hops, data_dir):
    url = "https://api.github.com/repos/own/src/releases/latest"
    hops.routes[url] = (302, {"Location": url + "?x=1"}, b"")
    hops.routes[url + "?x=1"] = (200, {}, b"{}")

    response, token_used = patcher.github_api_get(url)

    assert response.status_code == 200 and token_used is False
    assert len(hops.seen) == 2
    assert no_authorization(hops)


def test_a_redirect_loop_ends(hops, tmp_path):
    hops.routes[ASSET] = (302, {"Location": ASSET}, b"")

    with pytest.raises(requests.exceptions.TooManyRedirects):
        patcher.download_with_progress(ASSET, tmp_path / "p.mpp")

    assert list(tmp_path.glob("*.mpp*")) == []


def test_a_redirect_to_plain_http_is_refused(hops, tmp_path):
    hops.routes[ASSET] = (302, {"Location": "http://github.com/p.mpp"}, b"")

    with pytest.raises(requests.exceptions.InvalidURL):
        patcher.download_with_progress(ASSET, tmp_path / "p.mpp")


# --- 11: every patch-list download has a byte limit ---

BIG = 40 * 1024 * 1024


def served_under(net: Net, limit: int) -> bool:
    return net.served <= limit + 2 * CHUNK


def test_a_named_list_asset_is_cut_off_at_the_named_limit(paths, monkeypatch, capsys):
    release = build_release("v2", [("patches-2.mpp", 10), ("patches.json", 100)])
    files = {
        asset_url(release, "patches-2.mpp"): b"mpp",
        asset_url(release, "patches.json"): BIG,
    }
    net = Net(release, files)
    monkeypatch.setattr(patcher.requests, "get", net.get)

    changed = patcher.check_and_download_release(REPO, paths, force=True)

    assert changed is False
    assert served_under(net, patcher.MAX_NAMED_LIST_BYTES)
    out = capsys.readouterr().out
    assert "patches.json" in out and "larger than" in out


def test_a_repo_list_is_cut_off_at_the_named_limit(paths, monkeypatch):
    release = build_release("v2", [("patches-2.mpp", 10)])
    files = {
        asset_url(release, "patches-2.mpp"): b"mpp",
        RAW_ROOT + "v2/patches-list.json": BIG,
    }
    net = Net(release, files)
    monkeypatch.setattr(patcher.requests, "get", net.get)

    assert patcher.check_and_download_release(REPO, paths, force=True) is False
    assert served_under(net, patcher.MAX_NAMED_LIST_BYTES)


def test_the_small_pass_enforces_its_bound_on_bytes_received(paths, monkeypatch):
    release = build_release("v2", [("patches-2.mpp", 10), ("liar.json", 1000)])
    files = {
        asset_url(release, "patches-2.mpp"): b"mpp",
        asset_url(release, "liar.json"): BIG,
    }
    net = Net(release, files)
    monkeypatch.setattr(patcher.requests, "get", net.get)

    patcher.check_and_download_release(REPO, paths, force=True)

    # The small pass stops at 256 KiB; the last-resort pass then stops at 2 MiB.
    assert served_under(
        net, patcher.MAX_UNNAMED_LIST_BYTES + patcher.MAX_OVERSIZED_LIST_BYTES
    )


def test_a_real_sized_list_is_not_refused(paths, monkeypatch):
    entry = {"name": "Big", "compatiblePackages": {}}
    big = json.dumps([entry] * 20000).encode()  # about 1 MiB
    assert len(big) > 600 * 1024
    release = build_release("v2", [("patches-2.mpp", 10), ("patches.json", len(big))])
    files = {
        asset_url(release, "patches-2.mpp"): b"mpp",
        asset_url(release, "patches.json"): big,
    }
    monkeypatch.setattr(patcher.requests, "get", Net(release, files).get)

    assert patcher.check_and_download_release(REPO, paths, force=True) is True


# --- 12: the list is found before the bundle is downloaded ---


def test_a_new_release_without_a_list_does_not_download_the_bundle(paths, monkeypatch):
    paths[".mpp"].write_text("old mpp")
    paths[".json"].write_text(OLD_LIST)
    patcher.save_local_versions({REPO: "v1"})
    release = build_release("v2", [("patches-2.mpp", 1000)])
    net = Net(release, {asset_url(release, "patches-2.mpp"): b"new"})
    monkeypatch.setattr(patcher.requests, "get", net.get)

    changed = patcher.check_and_download_release(REPO, paths, force=True)

    assert changed is False
    assert asset_url(release, "patches-2.mpp") not in net.urls
    assert paths[".mpp"].read_text() == "old mpp"


def test_the_bundle_is_downloaded_after_the_list_was_found(paths, monkeypatch):
    release = build_release("v2", [("patches-2.mpp", 10), ("patches-list.json", 90)])
    files = {
        asset_url(release, "patches-2.mpp"): b"new",
        asset_url(release, "patches-list.json"): GOOD_LIST.encode(),
    }
    net = Net(release, files)
    monkeypatch.setattr(patcher.requests, "get", net.get)

    assert patcher.check_and_download_release(REPO, paths, force=True) is True
    list_at = net.urls.index(asset_url(release, "patches-list.json"))
    assert net.urls.index(asset_url(release, "patches-2.mpp")) > list_at


# --- 13: an interrupted commit forces the next sync to fetch again ---


def new_release_files() -> tuple[dict, dict]:
    release = build_release("v2", [("patches-2.mpp", 10), ("patches-list.json", 90)])
    files = {
        asset_url(release, "patches-2.mpp"): b"new mpp",
        asset_url(release, "patches-list.json"): GOOD_LIST.encode(),
    }
    return release, files


def test_a_failed_second_move_is_repaired_by_the_next_sync(paths, monkeypatch):
    paths[".mpp"].write_text("old mpp")
    paths[".json"].write_text(OLD_LIST)
    # A recent check: without a marker the next unforced sync would be throttled.
    patcher.save_local_versions({REPO: "v1", f"{REPO}#last_check": time.time()})
    release, files = new_release_files()
    net = Net(release, files)
    monkeypatch.setattr(patcher.requests, "get", net.get)
    real_replace = os.replace
    moves: list[str] = []

    def failing_second(src, dst) -> None:
        if str(src).endswith(".staged"):
            moves.append(str(dst))
            if len(moves) == 2:
                raise OSError("disk went away")
        real_replace(src, dst)

    monkeypatch.setattr(patcher.os, "replace", failing_second)
    first = patcher.check_and_download_release(REPO, paths, force=True)
    monkeypatch.setattr(patcher.os, "replace", real_replace)

    assert first is False
    assert patcher.load_local_versions()[REPO] == "v1"

    second = patcher.check_and_download_release(REPO, paths, force=False)

    assert second is True
    assert paths[".mpp"].read_text() == "new mpp"
    assert patcher.load_patch_list(REPO)[0]["name"] == "Good"
    versions = patcher.load_local_versions()
    assert versions[REPO] == "v2"
    assert not any(key.endswith("#incomplete") for key in versions)


def test_a_normal_sync_leaves_no_marker_and_records_the_tag(paths, monkeypatch):
    release, files = new_release_files()
    monkeypatch.setattr(patcher.requests, "get", Net(release, files).get)

    assert patcher.check_and_download_release(REPO, paths, force=True) is True

    versions = patcher.load_local_versions()
    assert versions[REPO] == "v2"
    assert set(versions) == {REPO, f"{REPO}#last_check"}


# --- 14: stale temp files are removed under the sync lock ---


def stale_files(directory) -> list:
    names = [
        "patches-own-src.mpp.0123abcd.part",
        "patches-own-src.json.staged",
        "patches-own-src.json.candidate",
        "patches-own-src.mpp.staged",
    ]
    files = [directory / name for name in names]
    for file in files:
        file.write_text("stale")
    return files


SYNC_CONFIG = {
    "repositories": {"cli": "own/cli"},
    "tools": {"cli_jar": "bin/cli.jar"},
    "sources": [],
}


def test_stale_download_files_are_removed_at_the_start_of_a_sync(data_dir, monkeypatch):
    bin_dir = data_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stale = stale_files(bin_dir)
    keep = bin_dir / "patches-own-src.json"
    keep.write_text("[]")
    monkeypatch.setattr(patcher, "check_and_download_release", lambda *a, **k: False)

    patcher.update_tools(dict(SYNC_CONFIG), quiet=True)

    assert not any(file.exists() for file in stale)
    assert keep.exists()


def test_files_of_a_running_sync_are_not_touched(data_dir, monkeypatch):
    bin_dir = data_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    inflight = stale_files(bin_dir)
    monkeypatch.setattr(patcher, "check_and_download_release", lambda *a, **k: False)

    with patcher.SYNC_LOCK:  # a download is in flight
        thread = threading.Thread(
            target=lambda: patcher.update_tools(dict(SYNC_CONFIG), quiet=True),
            daemon=True,
        )
        thread.start()
        time.sleep(0.2)
        still_there = all(file.exists() for file in inflight)
    join(thread)

    assert still_there
    assert not any(file.exists() for file in inflight)


# --- 15: a non-text recorded tag does not break the version string ---


@pytest.mark.parametrize("tag", [None, 5, ["v1"]])
def test_a_non_text_tag_still_gives_a_version_string(tag):
    version = patcher.build_combined_version("1.0", {"own/src": tag}, {})

    assert version.startswith("1.0-") and "/" not in version


def test_ordinary_tags_give_the_same_strings_as_before():
    version = patcher.build_combined_version("1.0", {"own/src": "v2.1+b"}, {})

    assert version.startswith("1.0-v2.1+b-")


# --- 16: literal forms of characters GitHub may leave unescaped ---

LITERAL_NAMES = ["a@b.mpp", "p(1).mpp", "a,b=c.mpp", "x!y*z'.mpp", "a$b&c;d:e.mpp"]


@pytest.mark.parametrize("name", LITERAL_NAMES)
def test_literal_and_encoded_forms_of_those_characters_are_accepted(name):
    from urllib.parse import quote

    base = "https://github.com/own/src/releases/download/v1/"

    assert patcher._is_release_asset_url(base + name, "v1", name) is True
    assert patcher._is_release_asset_url(base + quote(name, safe=""), "v1", name)


@pytest.mark.parametrize(
    "tail",
    ["x%2Empp", "x.mpp%00", "x.mpp/", "x.mpp?a=1", "x.mpp#f", "x%252Empp", "x\\.mpp"],
)
def test_other_forms_of_the_name_are_still_refused(tail):
    url = "https://github.com/own/src/releases/download/v1/" + tail

    assert patcher._is_release_asset_url(url, "v1", "x.mpp") is False


def test_a_name_with_a_slash_or_percent_never_matches_a_literal_form():
    base = "https://github.com/own/src/releases/download/v1/"

    assert not patcher._is_release_asset_url(base + "a/b.mpp", "v1", "a/b.mpp")
    assert not patcher._is_release_asset_url(base + "a%2Fb.mpp", "v1", "a/b.mpp")
    assert patcher._is_release_asset_url(base + "a%25b.mpp", "v1", "a%b.mpp")
    assert not patcher._is_release_asset_url(base + "a%b.mpp", "v1", "a%b.mpp")


# --- 18: every guard in the worker's close path is exercised ---


def test_a_close_that_raises_something_the_except_does_not_absorb_still_drains(
    monkeypatch,
):
    crashes: list[BaseException | None] = []
    monkeypatch.setattr(
        threading, "excepthook", lambda args: crashes.append(args.exc_value)
    )
    from tests.test_scraper_worker_resilience import (
        WAIT,
        FakeClock,
        FakeLauncher,
        start_worker,
    )

    launcher = FakeLauncher()
    worker = start_worker(launcher, FakeClock())

    def exits() -> None:
        raise SystemExit(2)

    launcher.browsers[0].close = exits  # type: ignore[method-assign]
    worker.shutdown()
    answer: main.queue.Queue = main.queue.Queue()
    worker.task_queue.put(main._Task(lambda scraper: "ran", (), {}, answer))
    worker.join(WAIT)

    _, error = answer.get(timeout=WAIT)
    assert isinstance(error, main.ScraperUnavailableError)
    assert len(crashes) == 1 and isinstance(crashes[0], SystemExit)  # not absorbed
