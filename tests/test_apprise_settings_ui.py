"""Settings UI for write-only Apprise notification destinations."""

import json
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from src import config_store, main

METHOD_END = "\n                },"
LEGACY_URL = "json://legacy-user:legacy-password@localhost"


@pytest.fixture
def page() -> str:
    return main.templates.get_template("index.html").render(
        config={
            "apps": [],
            "sources": [],
            "settings": {"server_url": "", "cron_schedule": ""},
        },
        available_apps=[],
        metadata={},
        request=None,
    )


def method_body(page: str, name: str) -> str:
    match = re.search(rf"\n {{16}}(?:async )?{name}\(", page)
    assert match is not None, f"{name} is not defined"
    return page[match.start() : page.index(METHOD_END, match.start())]


def script_of(page: str) -> str:
    match = re.search(r"<script>(.*?)</script>", page, re.DOTALL)
    assert match is not None
    return match.group(1)


PRELUDE = """
const handlers = {};
let factory;
const calls = [];
let reply = () => { throw new TypeError('no reply'); };
global.document = {addEventListener: (name, fn) => { handlers[name] = fn; }};
global.Alpine = {data: (name, fn) => { factory = fn; }};
global.location = {protocol: 'https:', hostname: 'example.test'};
global.sessionStorage = {getItem: () => null, removeItem() {}, setItem() {}};
global.confirm = () => true;
global.alert = () => { throw new Error('alert called'); };
global.setInterval = () => 0;
global.setTimeout = () => 0;
global.fetch = async (url, options = {}) => { calls.push({url, ...options}); return reply(url, options); };
function respond(status, body, headers = {}) {
  const lower = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return {ok: status < 300, status, headers: {get: k => lower[k.toLowerCase()] ?? null},
          json: async () => { if (body === undefined) throw new SyntaxError('no body'); return body; }};
}
"""

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed"
)


def run_js(page: str, tmp_path, steps: str) -> dict:
    program = (
        PRELUDE
        + script_of(page)
        + "\nconst setReply = fn => { reply = fn; };\n"
        + "handlers['alpine:init']();\nconst app = factory();\n"
        + f"(async () => {{ {steps} }})().then(r => {{ console.log(JSON.stringify(r)); process.exit(0); }},"
        + " e => { console.error(String(e)); process.exit(1); });\n"
    )
    path = tmp_path / "apprise-page.js"
    path.write_text(program)
    done = subprocess.run(
        ["node", str(path)], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


UNSET_STATUS = {
    "set": False,
    "count": 0,
    "source": None,
    "legacy_available": False,
    "migration_state": "none",
}


@pytest.fixture
def apprise_map():
    return {
        "github-token": {"set": False, "source": None},
        "cloakbrowser-license-key": {"set": False, "source": None},
        "apprise-urls": UNSET_STATUS,
    }


def test_settings_no_longer_render_legacy_notification_destinations(page):
    assert "settings.notification_urls" not in page
    assert "newNotificationUrl" not in page
    assert "Apprise Notifications" in page
    assert 'autocomplete="off"' in page
    assert 'x-text="appriseStatus.count"' in page
    assert "appriseStatus.source === 'file_invalid'" in page
    assert "appriseLocked" in page
    for state in ("pending", "conflict", "retry"):
        assert f"migration_state === '{state}'" in page


def test_status_map_includes_write_only_apprise_state(page):
    body = method_body(page, "appriseStatusValid")
    assert "migration_state" in body
    assert "legacy_available" in body
    assert "apprise-urls" in method_body(page, "secretStatusValid")


@needs_node
def test_legacy_status_count_can_exceed_the_new_list_limit(page, tmp_path):
    result = run_js(
        page,
        tmp_path,
        "return app.appriseStatusValid({set: true, count: 20, source: 'legacy', "
        "legacy_available: true, migration_state: 'pending'});",
    )
    assert result is True


def test_general_settings_and_notification_test_never_carry_destination_values(page):
    assert "notification_urls" not in method_body(page, "saveSettings")
    body = method_body(page, "testNotifications")
    assert "this.request('POST', '/api/ui/test_notification')" in body
    assert "notification_urls" not in body
    assert "JSON.stringify(this.settings)" not in body


def test_legacy_status_and_page_do_not_expose_destination_values(data_dir):
    config_store.update_config(
        lambda config: config["settings"].update(notification_urls=[LEGACY_URL])
    )
    client = TestClient(main.app)

    status = client.get("/api/ui/secrets").json()["apprise-urls"]
    page = client.get("/").text

    assert status == {
        "set": True,
        "count": 1,
        "source": "legacy",
        "legacy_available": True,
        "migration_state": "pending",
    }
    assert LEGACY_URL not in json.dumps(status)
    assert LEGACY_URL not in page


@needs_node
def test_save_replaces_destinations_in_order_without_echoing_or_starting_work(
    page, tmp_path, apprise_map
):
    apprise_map["apprise-urls"] = {
        "set": True,
        "count": 2,
        "source": "file",
        "legacy_available": False,
        "migration_state": "none",
    }
    result = run_js(
        page,
        tmp_path,
        f"setReply(() => respond(200, {json.dumps(apprise_map)}));"
        " app.appriseLoaded = true; app.appriseDraft = ['json://one:secret@host', 'json://two:secret@host'];"
        " await app.saveAppriseUrls();"
        " return {calls, draft: app.appriseDraft, input: app.appriseInput, message: app.appriseMessage};",
    )

    assert [call["url"] for call in result["calls"]] == ["/api/ui/secrets/apprise-urls"]
    assert result["calls"][0]["method"] == "PUT"
    assert json.loads(result["calls"][0]["body"]) == {
        "value": ["json://one:secret@host", "json://two:secret@host"]
    }
    assert result["draft"] == []
    assert result["input"] == ""
    assert result["message"]["kind"] == "ok"


@needs_node
def test_failed_save_keeps_destination_draft_and_typed_value(
    page, tmp_path, apprise_map
):
    result = run_js(
        page,
        tmp_path,
        "setReply(() => respond(500, {detail: 'Could not store notification URLs'}));"
        " app.appriseLoaded = true;"
        " app.appriseDraft = ['json://draft:secret@host']; app.appriseInput = 'json://new:secret@host';"
        " await app.saveAppriseUrls();"
        " return {draft: app.appriseDraft, input: app.appriseInput, message: app.appriseMessage};",
    )
    assert result["draft"] == ["json://draft:secret@host"]
    assert result["input"] == "json://new:secret@host"
    assert result["message"]["kind"] == "error"


@needs_node
def test_environment_conflict_discards_draft_and_refreshes_lock_status(
    page, tmp_path, apprise_map
):
    apprise_map["apprise-urls"] = {
        "set": False,
        "count": 0,
        "source": "environment_invalid",
        "legacy_available": False,
        "migration_state": "none",
    }
    result = run_js(
        page,
        tmp_path,
        f"setReply((url, options) => options.method === 'PUT'"
        f" ? respond(409, {{detail: 'Notification URLs are set by the environment'}})"
        f" : respond(200, {json.dumps(apprise_map)}));"
        " app.appriseLoaded = true;"
        " app.appriseDraft = ['json://draft:secret@host']; app.appriseInput = 'json://new:secret@host';"
        " await app.saveAppriseUrls();"
        " return {calls, draft: app.appriseDraft, input: app.appriseInput, status: app.appriseStatus};",
    )
    assert len(result["calls"]) == 2
    assert result["calls"][1]["url"] == "/api/ui/secrets"
    assert result["draft"] == [] and result["input"] == ""
    assert result["status"]["source"] == "environment_invalid"


@needs_node
@pytest.mark.parametrize("migration_state", ["pending", "retry"])
def test_legacy_migration_requires_explicit_confirm_and_has_no_url_payload(
    page, tmp_path, apprise_map, migration_state
):
    status = {
        "set": True,
        "count": 2,
        "source": "legacy" if migration_state == "pending" else "file",
        "legacy_available": True,
        "migration_state": migration_state,
    }
    apprise_map["apprise-urls"] = {
        "set": True,
        "count": 2,
        "source": status["source"],
        "legacy_available": True,
        "migration_state": migration_state,
    }
    result = run_js(
        page,
        tmp_path,
        f"setReply(() => respond(200, {json.dumps(apprise_map)}));"
        f" app.appriseLoaded = true; app.appriseStatus = {json.dumps(status)};"
        " await app.migrateAppriseUrls(); return calls;",
    )
    assert len(result) == 1
    assert result[0]["url"] == "/api/ui/secrets/apprise-urls/migration"
    assert result[0]["method"] == "POST"
    assert result[0].get("body") is None


@needs_node
def test_migration_conflict_is_not_retried_automatically(page, tmp_path):
    result = run_js(
        page,
        tmp_path,
        "app.appriseLoaded = true; app.appriseStatus = {set: true, count: 1, source: 'file', legacy_available: true, migration_state: 'conflict'};"
        " await app.migrateAppriseUrls(); return calls.length;",
    )
    assert result == 0


@needs_node
def test_notification_test_uses_saved_destinations_without_url_payload(
    page, tmp_path, apprise_map
):
    apprise_map["apprise-urls"] = {
        "set": True,
        "count": 1,
        "source": "file",
        "legacy_available": False,
        "migration_state": "none",
    }
    result = run_js(
        page,
        tmp_path,
        "app.appriseLoaded = true; app.appriseStatus = {set: true, count: 1, source: 'file', legacy_available: false, migration_state: 'none'};"
        " setReply(() => respond(200, {status: 'success'}));"
        " await app.testNotifications(); return {calls, notificationMessage: app.notificationMessage};",
    )
    assert result["calls"][0]["url"] == "/api/ui/test_notification"
    assert result["calls"][0]["method"] == "POST"
    assert result["calls"][0].get("body") is None
    assert result["notificationMessage"]["kind"] == "ok"


@needs_node
def test_clear_confirmation_then_clears_secret_and_legacy_state(
    page, tmp_path, apprise_map
):
    result = run_js(
        page,
        tmp_path,
        f"setReply(() => respond(200, {json.dumps(apprise_map)}));"
        " app.appriseLoaded = true; app.appriseStatus = {set: true, count: 1, source: 'file', legacy_available: false, migration_state: 'none'};"
        " app.appriseDraft = ['json://draft:secret@host']; app.appriseInput = 'json://typed:secret@host';"
        " await app.clearAppriseUrls();"
        " return {calls, draft: app.appriseDraft, input: app.appriseInput, message: app.appriseMessage};",
    )
    assert result["calls"][0]["url"] == "/api/ui/secrets/apprise-urls"
    assert result["calls"][0]["method"] == "DELETE"
    assert result["draft"] == [] and result["input"] == ""
    assert result["message"]["kind"] == "ok"


@needs_node
def test_environment_locked_status_cannot_save_clear_or_test(page, tmp_path):
    result = run_js(
        page,
        tmp_path,
        "app.appriseLoaded = true; app.appriseStatus = {set: false, count: 0, source: 'environment_invalid', legacy_available: true, migration_state: 'conflict'};"
        " return {locked: app.appriseLocked, canTest: app.appriseStatus.set};",
    )
    assert result == {"locked": True, "canTest": False}


@needs_node
def test_environment_locked_actions_make_no_requests(page, tmp_path):
    result = run_js(
        page,
        tmp_path,
        "app.appriseLoaded = true; app.appriseStatus = {set: false, count: 0, source: 'environment', legacy_available: true, migration_state: 'conflict'};"
        " app.appriseDraft = ['json://draft:secret@host'];"
        " await app.saveAppriseUrls(); await app.clearAppriseUrls();"
        " await app.migrateAppriseUrls(); await app.testNotifications(); return calls.length;",
    )
    assert result == 0


@needs_node
def test_malformed_apprise_status_success_preserves_existing_status_and_draft(
    page, tmp_path
):
    response = {
        "github-token": {"set": False, "source": None},
        "cloakbrowser-license-key": {"set": False, "source": None},
        "apprise-urls": {
            "set": True,
            "count": "one",
            "source": "file",
            "legacy_available": False,
            "migration_state": "none",
        },
    }
    result = run_js(
        page,
        tmp_path,
        f"setReply(() => respond(200, {json.dumps(response)}));"
        " app.appriseLoaded = true; app.appriseStatus = {set: false, count: 0, source: null, legacy_available: false, migration_state: 'none'};"
        " app.appriseDraft = ['json://draft:secret@host']; app.appriseInput = 'json://typed:secret@host';"
        " await app.saveAppriseUrls();"
        " return {status: app.appriseStatus, draft: app.appriseDraft, input: app.appriseInput, message: app.appriseMessage};",
    )
    assert result["status"]["source"] is None
    assert result["draft"] == ["json://draft:secret@host"]
    assert result["input"] == "json://typed:secret@host"
    assert result["message"]["kind"] == "error"
