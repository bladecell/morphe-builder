"""The Settings secrets card and the status notices, checked on the rendered page.

No test here runs JavaScript: they read the HTML and the Alpine script the page
serves. Behaviour in a browser is not covered (see the verification notes).
"""

import json
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from src import main

TOKEN = "ghp_exampleExampleExample1234"
LICENSE_KEY = "fake-license-key-example-1234"
METHOD_END = "\n                },"


@pytest.fixture
def client(data_dir, monkeypatch) -> TestClient:
    monkeypatch.setattr(main, "update_tools_task", lambda *args, **kwargs: None)
    (data_dir / "apks" / "icons").mkdir(parents=True)
    return TestClient(main.app)


@pytest.fixture
def page() -> str:
    template = main.templates.get_template("index.html")
    return template.render(
        config={"apps": [], "sources": []}, available_apps=[], metadata={}, request=None
    )


def method_body(page: str, name: str) -> str:
    """Return the source of one Alpine method or getter, or fail."""
    match = re.search(rf"\n {{16}}(?:async |get )?{name}\(", page)
    assert match is not None, f"{name} is not defined"
    end = page.index(METHOD_END, match.start())
    return page[match.start() : end]


def script_of(page: str) -> str:
    """Return the inline Alpine script of the page."""
    match = re.search(r"<script>(.*?)</script>", page, re.DOTALL)
    assert match is not None
    return match.group(1)


PRELUDE = """
const handlers = {};
let factory;
const calls = [];
let reply = () => { throw new TypeError('no reply set'); };
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
    """Run the page script in node with a fake fetch and return what `steps` result holds.

    `steps` is an async function body with `app` (the component object), `calls`,
    `respond(status, body, headers)` and `setReply(fn)`; it returns a JSON value.
    """
    program = (
        PRELUDE
        + script_of(page)
        + "\nconst setReply = fn => { reply = fn; };\n"
        + "handlers['alpine:init']();\nconst app = factory();\n"
        + f"(async () => {{ {steps} }})().then(r => {{ console.log(JSON.stringify(r)); process.exit(0); }},"
        + " e => { console.error(String(e)); process.exit(1); });\n"
    )
    path = tmp_path / "page.js"
    path.write_text(program)
    done = subprocess.run(
        ["node", str(path)], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def secrets_card(page: str) -> str:
    start = page.index("<!-- Secrets card -->")
    return page[start : page.index("<!-- System settings card -->", start)]


# --- the card -----------------------------------------------------------------------


def test_both_supported_secrets_have_settings_fields(page):
    assert "secretFields" in page
    assert "'github-token'" in page
    assert "'cloakbrowser-license-key'" in page
    assert "CLOAKBROWSER_LICENSE_KEY" in page


def test_secret_input_is_a_password_field_that_password_managers_leave_alone(page):
    card = secrets_card(page)

    assert 'type="password"' in card
    assert (
        ":autocomplete=\"field.slug === 'cloakbrowser-license-key' ? 'off' : 'new-password'\""
        in card
    )
    assert 'spellcheck="false"' in card
    assert 'autocapitalize="off"' in card


def test_cloakbrowser_key_password_manager_autocomplete_is_off(page):
    card = secrets_card(page)
    assert "field.slug === 'cloakbrowser-license-key'" in card
    assert "field.slug === 'cloakbrowser-license-key' ? 'off' : 'new-password'" in card


def test_card_has_no_form_and_the_input_is_labelled_and_described(page):
    card = secrets_card(page)

    assert "<form" not in card
    assert "<label" in card
    assert "aria-describedby" in card
    assert 'role="alert"' in card


def test_card_renders_every_state_of_a_secret(page):
    card = secrets_card(page)

    for state in ("loading", "unset", "file", "environment", "environment_invalid"):
        assert f"'{state}'" in card, state
    assert "Not set" in card
    assert "Set by environment" in card
    assert "Environment value invalid" in card


def test_card_shows_the_plain_http_hint_only_over_http(page):
    card = secrets_card(page)

    assert "plainHttp" in card
    assert "unencrypted" in card
    assert "location.protocol" in page


def test_card_shows_warning_and_rejection_text_from_the_status(page):
    card = secrets_card(page)

    assert "githubTokenWarning" in card
    assert "githubTokenRejected" in card


def test_cloakbrowser_rejection_is_a_separate_last_known_browser_status(page):
    card = secrets_card(page)
    assert "browser.license_key_rejected" in card
    assert "last browser result" in card
    assert "next scraper task" in card


def test_card_comes_before_system_settings(page):
    assert page.index("<!-- Secrets card -->") < page.index(
        "<!-- System settings card -->"
    )


# --- methods ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["request", "loadSecrets", "saveSecret", "clearSecret", "secretState"]
)
def test_secret_methods_exist(page, name):
    method_body(page, name)


@pytest.mark.parametrize(
    "name", ["request", "loadSecrets", "saveSecret", "clearSecret"]
)
def test_secret_methods_never_alert_log_or_store(page, name):
    body = method_body(page, name)

    for banned in ("alert(", "console.", "sessionStorage", "localStorage"):
        assert banned not in body, banned


def test_secret_value_travels_only_in_the_put_body(page):
    body = method_body(page, "saveSecret")

    assert "'PUT'" in body
    assert "`/api/ui/secrets/${slug}`, {value}" in body
    assert "location" not in body


def test_clear_asks_first_and_uses_delete(page):
    body = method_body(page, "clearSecret")

    assert "confirm(" in body
    assert "'DELETE'" in body


# --- status polling and notices -----------------------------------------------------


def test_tools_status_polling_checks_the_response_before_reading_it(page):
    body = method_body(page, "checkToolsStatus")

    assert "response.ok" in body
    assert "github_token_warning" in body
    assert "github_token_rejected" in body
    assert "github_token_permissions" in body
    assert "server_time" in body


def test_discovery_refresh_keeps_the_list_on_a_refused_or_repeated_sync(page):
    body = method_body(page, "refreshDiscovery")

    assert "response.ok" in body
    assert "already running" in body
    assert body.index("response.ok") < body.index("data.apps")


def test_already_running_is_a_neutral_notice_not_the_red_sync_error(page):
    assert "syncNotice" in page
    notice = re.search(r"x-if=\"syncNotice\".{0,300}", page, re.DOTALL)
    assert notice is not None
    assert "red-" not in notice.group(0)
    assert "A sync is already running" in page


def test_rejected_token_is_not_shown_twice_on_the_discovery_tab(page):
    assert re.search(r"syncError\s*&&\s*syncError\s*!==\s*githubTokenRejected", page)


# --- header -------------------------------------------------------------------------


def test_scraper_diagnostic_tones_remain_without_header_badges(page):
    body = method_body(page, "scraperTone")
    header = page[page.index("<header") : page.index("</header>")]
    text = method_body(page, "scraperTextClass")
    dot = method_body(page, "scraperDotClass")

    assert "'available'" in body
    assert "'starting'" in body and "'stopping'" in body
    assert "'amber'" in body and "'emerald'" in body and "'red'" in body
    assert (
        "text-emerald-500" in text
        and "text-amber-500" in text
        and "text-red-500" in text
    )
    assert "bg-amber-500" in dot
    assert "bg-emerald-500" in dot and "bg-red-500" in dot
    assert "Java" not in header and "Scraper" not in header
    assert "scraperTextClass" not in header and "scraperDotClass" not in header


def test_busy_task_older_than_ten_minutes_is_flagged_using_server_time(page):
    assert "stuckAfterSeconds: 600" in page
    body = method_body(page, "busySeconds")

    assert "busy_since" in body
    assert "serverClockOffset" in page
    assert "Date.now()" in page


def test_settings_tab_shows_a_dot_for_a_rejected_or_warned_token(page):
    tab = page[page.index("@click=\"activeTab = 'settings'\"") :]
    tab = tab[: tab.index("</button>")]

    assert "githubTokenRejected" in tab
    assert "githubTokenWarning" in tab


# --- no secret reaches the page -----------------------------------------------------


def test_page_never_carries_a_stored_secret_or_its_status(client):
    assert (
        client.put("/api/ui/secrets/github-token", json={"value": TOKEN}).status_code
        == 200
    )

    html = client.get("/").text

    assert TOKEN not in html
    assert '"github-token": {' not in html
    assert "ghp_" not in html

    assert (
        client.put(
            "/api/ui/secrets/cloakbrowser-license-key", json={"value": LICENSE_KEY}
        ).status_code
        == 200
    )
    html = client.get("/").text
    assert LICENSE_KEY not in html
    assert '"cloakbrowser-license-key": {' not in html


def test_page_stores_only_the_active_tab_in_session_storage(page):
    uses = re.findall(r"(?:session|local)Storage\.\w+\(([^)]*)\)", page)

    assert uses
    assert all("activeTab" in use for use in uses)


def test_locked_states_hide_the_input_row_and_keep_it_in_the_page(page):
    card = secrets_card(page)
    input_at = re.search(r"<input[^>]*type=\"password\"", card).start()
    row = re.findall(
        r"<div class=\"space-y-2\"[^>]*x-show=\"([^\"]*)\"", card[:input_at]
    )

    assert row, "the input row is toggled with x-show so a save keeps focus"
    assert "'unset'" in row[-1] and "'file'" in row[-1]
    assert "environment" not in row[-1] and "loading" not in row[-1]


def test_static_hint_does_not_promise_a_check_that_may_not_happen(page):
    assert "checked by a sync right away" not in page
    assert "starts a sync when none is running" in page


@needs_node
def test_saving_cloakbrowser_key_uses_only_secret_route_and_does_not_start_work(
    page, tmp_path
):
    result = run_js(
        page,
        tmp_path,
        "setReply(() => respond(200, {'github-token': {set: false, source: null}, "
        "'cloakbrowser-license-key': {set: true, source: 'file'}, "
        "'apprise-urls': {set: false, count: 0, source: null, legacy_available: false, migration_state: 'none'}}));"
        " app.secretsLoaded = true; app.secretInput['cloakbrowser-license-key'] = 'secretvalue123';"
        " await app.saveSecret('cloakbrowser-license-key');"
        " return {calls, message: app.secretMessage['cloakbrowser-license-key'], "
        "input: app.secretInput['cloakbrowser-license-key']};",
    )
    assert [call["url"] for call in result["calls"]] == [
        "/api/ui/secrets/cloakbrowser-license-key",
        "/api/ui/tools_status",
    ]
    assert result["calls"][0]["url"] == "/api/ui/secrets/cloakbrowser-license-key"
    assert result["calls"][0]["method"] == "PUT"
    assert (
        result["message"]["text"]
        == "Saved. The browser will use the new key before its next task."
    )
    assert result["input"] == ""


@needs_node
def test_cloakbrowser_last_known_rejection_remains_independent_of_saved_state(
    page, tmp_path
):
    result = run_js(
        page,
        tmp_path,
        "app.browser.license_key_rejected = true;"
        " app.secretsLoaded = true; app.secrets['cloakbrowser-license-key'] = {set: true, source: 'file'};"
        " return {state: app.secretState('cloakbrowser-license-key'), "
        "rejected: app.browser.license_key_rejected};",
    )
    assert result == {"state": "file", "rejected": True}


@needs_node
@pytest.mark.parametrize("status", [400, 403, 500])
def test_cloakbrowser_write_failure_keeps_typed_value(page, tmp_path, status):
    result = run_js(
        page,
        tmp_path,
        f"setReply(() => respond({status}, {{detail: '<b>refused</b>'}}));"
        " app.secretsLoaded = true; app.secretInput['cloakbrowser-license-key'] = 'secretvalue123';"
        " await app.saveSecret('cloakbrowser-license-key');"
        " return {input: app.secretInput['cloakbrowser-license-key'], "
        "message: app.secretMessage['cloakbrowser-license-key']};",
    )
    assert result["input"] == "secretvalue123"
    assert result["message"]["kind"] == "error"
    assert result["message"]["text"] == "<b>refused</b>"


@needs_node
def test_cloakbrowser_environment_conflict_clears_draft_and_refreshes_status(
    page, tmp_path
):
    result = run_js(
        page,
        tmp_path,
        "setReply((url, options) => options.method === 'PUT'"
        " ? respond(409, {detail: 'Environment owns this secret'})"
        " : respond(200, {'github-token': {set: false, source: null}, "
        "'cloakbrowser-license-key': {set: false, source: 'environment_invalid'}, "
        "'apprise-urls': {set: false, count: 0, source: null, legacy_available: false, migration_state: 'none'}}));"
        " app.secretsLoaded = true; app.secretInput['cloakbrowser-license-key'] = 'secretvalue123';"
        " await app.saveSecret('cloakbrowser-license-key');"
        " return {calls, input: app.secretInput['cloakbrowser-license-key'], "
        "state: app.secretState('cloakbrowser-license-key')};",
    )
    assert len(result["calls"]) == 2
    assert result["calls"][1]["url"] == "/api/ui/secrets"
    assert result["input"] == ""
    assert result["state"] == "environment_invalid"


@needs_node
def test_cloakbrowser_network_failure_keeps_the_draft(page, tmp_path):
    result = run_js(
        page,
        tmp_path,
        "setReply(() => { throw new TypeError('offline'); });"
        " app.secretInput['cloakbrowser-license-key'] = 'secretvalue123';"
        " await app.saveSecret('cloakbrowser-license-key');"
        " return {input: app.secretInput['cloakbrowser-license-key'], "
        "message: app.secretMessage['cloakbrowser-license-key']};",
    )
    assert result["input"] == "secretvalue123"
    assert result["message"] == {
        "kind": "error",
        "text": "Could not reach the server.",
    }


@needs_node
@pytest.mark.parametrize(
    "body",
    [
        "undefined",
        "null",
        json.dumps({"github-token": {"set": False, "source": None}}),
    ],
)
def test_malformed_successful_secret_load_preserves_current_status(
    page, tmp_path, body
):
    result = run_js(
        page,
        tmp_path,
        f"setReply(() => respond(200, {body}));"
        " app.secretsLoaded = true; app.secrets = {'cloakbrowser-license-key': {set: true, source: 'file'}};"
        " await app.loadSecrets();"
        " return {loaded: app.secretsLoaded, state: app.secretState('cloakbrowser-license-key'), error: app.secretsLoadError};",
    )
    assert result == {
        "loaded": True,
        "state": "file",
        "error": "The server returned an invalid secret status.",
    }


@needs_node
@pytest.mark.parametrize("method", ["saveSecret", "clearSecret"])
@pytest.mark.parametrize(
    "body",
    [
        "undefined",
        "null",
        json.dumps({"cloakbrowser-license-key": {"set": "yes", "source": "file"}}),
    ],
)
def test_malformed_successful_secret_mutation_preserves_state_and_draft(
    page, tmp_path, method, body
):
    result = run_js(
        page,
        tmp_path,
        f"setReply(() => respond(200, {body}));"
        " app.secretsLoaded = true; app.secrets = {'github-token': {set: false, source: null}, 'cloakbrowser-license-key': {set: false, source: null}};"
        " app.secretInput['cloakbrowser-license-key'] = 'draft-value-123';"
        f" await app.{method}('cloakbrowser-license-key');"
        " return {state: app.secretState('cloakbrowser-license-key'), input: app.secretInput['cloakbrowser-license-key'], "
        "message: app.secretMessage['cloakbrowser-license-key'], calls};",
    )
    assert result["state"] == "unset"
    assert result["input"] == "draft-value-123"
    assert result["message"]["kind"] == "error"
    assert result["message"]["text"] == "The server returned an invalid secret status."
    assert len(result["calls"]) == 1


def test_unverifiable_permissions_text_is_generic(page):
    card = secrets_card(page)

    assert (
        "This kind of token does not report its permissions, so they cannot be "
        "checked here." in card
    )
    for value in ("none", "present", "unverifiable", "unchecked"):
        assert f"githubTokenPermissions === '{value}'" in card


# --- behaviour, executed in node (skipped without node) ------------------------------


@needs_node
@pytest.mark.parametrize(
    ("state", "diagnostics", "tone"),
    [
        ("available", True, "emerald"),
        ("available", False, "red"),
        ("starting", False, "amber"),
        ("starting", True, "amber"),
        ("stopping", True, "amber"),
        ("unavailable", True, "red"),
        ("stopped", True, "red"),
    ],
)
def test_scraper_tone_by_worker_state(page, tmp_path, state, diagnostics, tone):
    result = run_js(
        page,
        tmp_path,
        f"app.browser = {{state: '{state}'}}; app.diagnostics = {{scraper: {json.dumps(diagnostics)}}};"
        " return [app.scraperTone, app.scraperTextClass, app.scraperDotClass];",
    )

    assert result[0] == tone
    assert f"text-{tone}-500" in result[1]
    assert f"bg-{tone}-500" in result[2]


@needs_node
@pytest.mark.parametrize(
    ("secrets", "expected"),
    [
        (None, "loading"),
        ({"s": {"set": False, "source": None}}, "unset"),
        ({"s": {"set": True, "source": "file"}}, "file"),
        ({"s": {"set": True, "source": "environment"}}, "environment"),
        ({"s": {"set": False, "source": "environment_invalid"}}, "environment_invalid"),
        ({}, "unset"),
    ],
)
def test_secret_state_from_the_status(page, tmp_path, secrets, expected):
    result = run_js(
        page,
        tmp_path,
        f"const s = {json.dumps(secrets)}; if (s) {{ app.secrets = s; app.secretsLoaded = true; }}"
        " return app.secretState('s');",
    )

    assert result == expected


@needs_node
def test_busy_seconds_uses_the_server_clock_and_flags_after_ten_minutes(page, tmp_path):
    result = run_js(
        page,
        tmp_path,
        "const serverNow = Date.now() / 1000 + 3600;"
        " app.serverClockOffset = serverNow * 1000 - Date.now();"
        " app.browser = {state: 'available', busy_since: serverNow - 700};"
        " const late = [app.busySeconds, app.busyStuck];"
        " app.browser = {state: 'available', busy_since: serverNow - 30};"
        " const early = [app.busySeconds, app.busyStuck];"
        " app.browser = {state: 'available', busy_since: null};"
        " return {late, early, idle: [app.busySeconds, app.busyStuck]};",
    )

    assert 699 <= result["late"][0] <= 702 and result["late"][1] is True
    assert 29 <= result["early"][0] <= 32 and result["early"][1] is False
    assert result["idle"] == [None, False]


SAVE_OUTCOMES = [
    ("queued", "being checked by a sync now"),
    ("already-running", "already running"),
    ("not-needed", "No change"),
    ("unavailable", "Use Sync Sources"),
    (None, "Saved."),
]


@needs_node
@pytest.mark.parametrize(("sync", "text"), SAVE_OUTCOMES)
def test_save_message_follows_the_sync_header_and_clears_the_input(
    page, tmp_path, sync, text
):
    headers = {} if sync is None else {"X-Sync": sync}
    result = run_js(
        page,
        tmp_path,
        "setReply(() => respond(200, {'github-token': {set: true, source: 'file'}, "
        "'cloakbrowser-license-key': {set: false, source: null}, "
        "'apprise-urls': {set: false, count: 0, source: null, legacy_available: false, migration_state: 'none'}}, "
        f"{json.dumps(headers)}));"
        " app.secretsLoaded = true; app.secretInput['github-token'] = 'abcdefgh12345';"
        " await app.saveSecret('github-token');"
        " return {message: app.secretMessage['github-token'], input: app.secretInput['github-token'],"
        " state: app.secretState('github-token'), call: calls[0]};",
    )

    assert result["message"]["kind"] == "ok"
    assert text in result["message"]["text"]
    assert result["input"] == ""
    assert result["state"] == "file"
    assert result["call"]["url"] == "/api/ui/secrets/github-token"
    assert result["call"]["method"] == "PUT"
    assert json.loads(result["call"]["body"]) == {"value": "abcdefgh12345"}


@needs_node
@pytest.mark.parametrize(
    ("sync", "text"),
    [
        ("queued", "A sync is running without the token"),
        ("already-running", "already running"),
        ("not-needed", "No change"),
        ("unavailable", "Use Sync Sources"),
        (None, "Removed."),
    ],
)
def test_clear_message_follows_the_sync_header(page, tmp_path, sync, text):
    headers = {} if sync is None else {"X-Sync": sync}
    result = run_js(
        page,
        tmp_path,
        "setReply(() => respond(200, {'github-token': {set: false, source: null}, "
        "'cloakbrowser-license-key': {set: false, source: null}, "
        "'apprise-urls': {set: false, count: 0, source: null, legacy_available: false, migration_state: 'none'}}, "
        f"{json.dumps(headers)}));"
        " await app.clearSecret('github-token');"
        " return {message: app.secretMessage['github-token'], call: calls[0]};",
    )

    assert result["message"]["kind"] == "ok"
    assert text in result["message"]["text"]
    assert result["call"]["method"] == "DELETE"


@needs_node
def test_clear_does_nothing_when_the_confirm_is_declined(page, tmp_path):
    result = run_js(
        page,
        tmp_path,
        "global.confirm = () => false; await app.clearSecret('github-token');"
        " return calls.length;",
    )

    assert result == 0


@needs_node
def test_environment_locked_clear_409_discards_typed_draft(page, tmp_path):
    result = run_js(
        page,
        tmp_path,
        "setReply(url => url.includes('/github-token')"
        " ? respond(409, {detail: 'Secret is set by the environment'})"
        " : respond(200, {'github-token': {set: true, source: 'environment'}, "
        "'cloakbrowser-license-key': {set: false, source: null}, "
        "'apprise-urls': {set: false, count: 0, source: null, legacy_available: false, migration_state: 'none'}}));"
        " app.secretInput['github-token'] = 'draft-token-12345';"
        " await app.clearSecret('github-token');"
        " return {input: app.secretInput['github-token'],"
        " state: app.secretState('github-token'),"
        " message: app.secretMessage['github-token']};",
    )

    assert result["input"] == ""
    assert result["state"] == "environment"
    assert result["message"]["kind"] == "error"


@needs_node
@pytest.mark.parametrize(
    ("status", "body", "kept", "fragment"),
    [
        (400, {"detail": "Invalid secret value"}, True, "8 to 512"),
        (
            403,
            {"detail": "Cross-site request refused (Sec-Fetch-Site)."},
            True,
            "Cross-site",
        ),
        (500, {"detail": "Could not write the secrets file"}, True, "Could not write"),
        (502, None, True, "HTTP 502"),
        (409, {"detail": "Secret is set by the environment"}, False, "environment"),
    ],
)
def test_failed_save_shows_the_detail_and_keeps_the_value_except_on_409(
    page, tmp_path, status, body, kept, fragment
):
    result = run_js(
        page,
        tmp_path,
        f"setReply(() => respond({status}, {json.dumps(body) if body else 'undefined'}));"
        " app.secretInput['github-token'] = 'abcdefgh12345';"
        " await app.saveSecret('github-token');"
        " return {message: app.secretMessage['github-token'], input: app.secretInput['github-token']};",
    )

    assert result["message"]["kind"] == "error"
    assert fragment in result["message"]["text"]
    assert (result["input"] == "abcdefgh12345") is kept
    assert "abcdefgh12345" not in json.dumps(result["message"])


@needs_node
def test_network_failure_on_save_is_shown_and_the_value_kept(page, tmp_path):
    result = run_js(
        page,
        tmp_path,
        "setReply(() => { throw new TypeError('Failed to fetch'); });"
        " app.secretInput['github-token'] = 'abcdefgh12345';"
        " await app.saveSecret('github-token');"
        " return {message: app.secretMessage['github-token'], input: app.secretInput['github-token']};",
    )

    assert result["message"] == {"kind": "error", "text": "Could not reach the server."}
    assert result["input"] == "abcdefgh12345"


@needs_node
def test_leaving_the_settings_tab_clears_the_typed_value_and_the_message(
    page, tmp_path
):
    result = run_js(
        page,
        tmp_path,
        "setReply(() => respond(200, {}));"
        " const watchers = {}; app.$watch = (key, fn) => { watchers[key] = fn; };"
        " app.init();"
        " app.secretInput['github-token'] = 'abcdefgh12345';"
        " app.secretMessage['github-token'] = {kind: 'ok', text: 'Saved.'};"
        " watchers.activeTab('pipeline');"
        " return [app.secretInput['github-token'], app.secretMessage['github-token']];",
    )

    assert result == ["", None]


@needs_node
def test_refused_sync_keeps_the_discovery_list_and_shows_the_detail(page, tmp_path):
    result = run_js(
        page,
        tmp_path,
        "app.discoveryApps = [{id: 'a'}];"
        " setReply(() => respond(403, {detail: 'Cross-site request refused'}));"
        " await app.refreshDiscovery(true);"
        " return [app.discoveryApps, app.syncRefused, app.syncNotice];",
    )

    assert result == [[{"id": "a"}], "Cross-site request refused", None]


@needs_node
def test_sync_already_running_is_a_notice_and_the_list_is_kept(page, tmp_path):
    result = run_js(
        page,
        tmp_path,
        "setReply(() => respond(200, {apps: [{id: 'b'}], sync: 'already running'}));"
        " await app.refreshDiscovery(true);"
        " return [app.discoveryApps, app.syncRefused, app.syncNotice];",
    )

    assert result == [[{"id": "b"}], None, "A sync is already running."]


@needs_node
def test_tools_status_fills_the_token_fields_and_the_clock_offset(page, tmp_path):
    result = run_js(
        page,
        tmp_path,
        "setReply(() => respond(200, {updating: false, sync_error: null,"
        " github_token_warning: 'warn', github_token_rejected: null,"
        " github_token_permissions: 'unverifiable', server_time: Date.now() / 1000 + 100,"
        " browser: {state: 'starting'}, diagnostics: {java: true, scraper: true}}));"
        " await app.checkToolsStatus();"
        " return [app.githubTokenWarning, app.githubTokenRejected, app.githubTokenPermissions,"
        " app.serverClockOffset > 99000, app.browser.state];",
    )

    assert result == ["warn", None, "unverifiable", True, "starting"]


@needs_node
def test_refused_tools_status_keeps_the_last_state(page, tmp_path):
    result = run_js(
        page,
        tmp_path,
        "app.githubTokenRejected = 'old'; setReply(() => respond(403, {detail: 'no'}));"
        " await app.checkToolsStatus(); return app.githubTokenRejected;",
    )

    assert result == "old"
