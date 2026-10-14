"""Browser regressions for the existing admin header and dialogs.

The local HTTP server serves the real rendered page and self-hosted assets. Its
API responses are fixtures; the test never starts the application worker or
connects to an external service.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Error, Page, expect, sync_playwright

from src import main

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ID = "org.example.fixture"


def render_page() -> bytes:
    """Render the real admin template with one local fixture application."""
    template = main.templates.get_template("index.html")
    return template.render(
        config={
            "apps": [{"id": PACKAGE_ID, "name": "Fixture App", "include_patches": []}],
            "sources": [
                {
                    "repo": "FixtureOrg/patches",
                    "name": "Fixture Source",
                    "version": "v1",
                    "active": True,
                }
            ],
            "settings": {
                "server_url": "",
                "cron_schedule": "",
            },
        },
        available_apps=[{"pkg": "org.example.newapp", "name": "New App"}],
        metadata={},
        request=None,
    ).encode()


class LocalHandler(BaseHTTPRequestHandler):
    """Serve the rendered page, vendored assets, and inert API fixtures."""

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self._respond(200, "text/html", render_page())
            return
        if path.startswith("/static/"):
            asset = (ROOT / "src" / path.lstrip("/")).resolve()
            if asset.is_relative_to(ROOT / "src" / "static") and asset.is_file():
                content_type = (
                    "text/css" if asset.suffix == ".css" else "text/javascript"
                )
                self._respond(200, content_type, asset.read_bytes())
                return
        payloads = {
            "/api/ui/build_status": {
                "in_progress": False,
                "message": "Idle",
                "last_run": "Never",
            },
            "/api/ui/tools_status": {
                "updating": False,
                "browser": {"state": "available"},
                "diagnostics": {"java": True, "scraper": True},
            },
            "/api/ui/updates": {
                PACKAGE_ID: {
                    "file_exists": True,
                    "icon_exists": False,
                    "has_update": False,
                    "status": "Complete",
                }
            },
            "/api/ui/secrets": {
                "github-token": {"set": False, "source": None},
                "apprise-urls": {
                    "set": False,
                    "count": 0,
                    "source": None,
                    "legacy_available": False,
                    "migration_state": "none",
                },
            },
            f"/api/ui/patches/{PACKAGE_ID}": {
                "sources": [
                    {
                        "repo": "FixtureOrg/patches",
                        "source": "Fixture Source",
                        "is_base": True,
                        "status": "available",
                        "patches": [
                            {
                                "name": "Fixture patch",
                                "description": "Fixture patch description",
                                "source": "Fixture Source",
                                "is_base": True,
                                "enabled_by_default": False,
                                "selected": False,
                            }
                        ],
                    }
                ],
                "selection": {
                    "stored": [],
                    "resolved": {},
                    "status": "empty",
                    "unresolved": [],
                    "error": None,
                },
            },
            "/api/ui/patches/org.example.newapp": {
                "sources": [
                    {
                        "repo": "FixtureOrg/patches",
                        "source": "Fixture Source",
                        "is_base": True,
                        "status": "available",
                        "patches": [
                            {
                                "name": "New app patch",
                                "description": "New app patch description",
                                "source": "Fixture Source",
                                "is_base": True,
                                "enabled_by_default": False,
                                "selected": False,
                            }
                        ],
                    }
                ],
                "selection": {
                    "stored": [],
                    "resolved": {},
                    "status": "empty",
                    "unresolved": [],
                    "error": None,
                },
            },
            "/api/ui/logs": {"logs": ""},
            "/api/ui/metadata": {},
        }
        payload = payloads.get(path)
        if payload is not None:
            self._respond(200, "application/json", json.dumps(payload).encode())
            return
        self._respond(404, "application/json", b"{}")

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def local_page():
    """Open the page from localhost, blocking every non-local browser request."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Error as error:
                pytest.skip(f"Local Chromium unavailable: {error}")
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.add_init_script(
                "window.__pageErrors=[];"
                "window.addEventListener('error', event => "
                "window.__pageErrors.push(event.message));"
            )
            page.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if route.request.url.startswith(base_url)
                    else route.abort()
                ),
            )
            page.goto(base_url, wait_until="load")
            page.wait_for_function("window.Alpine && Alpine.$data(document.body)")
            yield page
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def document_width(page: Page) -> int:
    return page.evaluate("document.documentElement.scrollWidth")


def test_header_reflows_without_mobile_horizontal_overflow(local_page: Page):
    for width in (390, 768, 1280):
        local_page.set_viewport_size({"width": width, "height": 844})
        assert document_width(local_page) <= width
        header = local_page.locator("header")
        expect(header.get_by_text("Java", exact=True)).to_have_count(0)
        expect(header.get_by_text("Scraper", exact=True)).to_have_count(0)
        expect(header.locator(":scope > div > div")).to_have_count(1)


def test_main_tabs_switch_to_visible_discovery_pipeline_and_settings_panels(
    local_page: Page,
):
    local_page.get_by_role("button", name="Discovery").click()
    expect(local_page.get_by_role("heading", name="Discovery")).to_be_visible()

    local_page.get_by_role("button", name="Pipeline").click()
    expect(local_page.get_by_role("heading", name="Active Pipeline")).to_be_visible()

    local_page.get_by_role("button", name="Settings").click()
    expect(local_page.get_by_role("heading", name="Secrets")).to_be_visible()
    expect(
        local_page.get_by_role("textbox", name="Notification destination URL")
    ).to_be_visible()


def test_pipeline_card_keeps_identity_actions_and_selection_separate_at_all_widths(
    local_page: Page,
):
    card = local_page.get_by_role("heading", name="Fixture App").locator(
        "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' group ')][1]"
    )
    long_name = "A fixture application name long enough to wrap on a phone"
    long_package = "org.example." + "long.package.segment." * 5
    long_error = "Fixture update error: " + "unavailable." * 12
    local_page.evaluate(
        """values => {
            const app = Alpine.$data(document.body);
            app.pipelineApps[0].name = values.name;
            app.updates['org.example.fixture'] = {
                file_exists: true,
                icon_exists: false,
                has_update: false,
                status: 'Error',
                error: values.error,
            };
            document.querySelector('main code').textContent = values.package;
            document.querySelector('[aria-label="App actions"]').closest('.group').querySelector('code').textContent = values.package;
        }""",
        {"name": long_name, "package": long_package, "error": long_error},
    )

    for width in (320, 390, 1280):
        local_page.set_viewport_size({"width": width, "height": 844})
        actions = card.get_by_role("group", name="App actions")
        expect(actions).to_be_visible()
        assert (
            actions.evaluate("element => getComputedStyle(element).position")
            != "absolute"
        )
        expect(actions.locator("button, a")).to_have_count(5)
        action_controls = actions.locator("button, a").all()
        action_centers_by_row = {}
        for control in action_controls:
            control_box = control.bounding_box()
            icon_box = control.locator("svg").bounding_box()
            assert control_box and icon_box
            assert (
                abs(
                    icon_box["x"]
                    + icon_box["width"] / 2
                    - control_box["x"]
                    - control_box["width"] / 2
                )
                <= 1
            )
            assert (
                abs(
                    icon_box["y"]
                    + icon_box["height"] / 2
                    - control_box["y"]
                    - control_box["height"] / 2
                )
                <= 1
            )
            row = round(control_box["y"])
            action_centers_by_row.setdefault(row, []).append(control_box)

        actions_box = actions.bounding_box()
        card_box = card.bounding_box()
        assert actions_box and card_box
        content_center_x = card_box["x"] + card_box["width"] / 2
        for row_controls in action_centers_by_row.values():
            row_left = min(box["x"] for box in row_controls)
            row_right = max(box["x"] + box["width"] for box in row_controls)
            assert abs((row_left + row_right) / 2 - content_center_x) <= 1
        for name in (
            "Add to Obtainium (App API)",
            "Open Download Page",
            "Upload Manual APK",
            "Edit Configuration",
            "Remove App",
        ):
            control = actions.get_by_role("button", name=name)
            if name == "Open Download Page":
                control = actions.get_by_role("link", name=name)
            expect(control).to_be_visible()
            assert control.evaluate(
                "element => element.getBoundingClientRect().width >= 44 && element.getBoundingClientRect().height >= 44"
            )

        identity = card.get_by_role("heading", name=long_name).locator("xpath=../..")
        icon = identity.locator("div").first
        assert icon.evaluate(
            "element => element.getBoundingClientRect().left"
        ) < card.get_by_role("heading", name=long_name).evaluate(
            "element => element.getBoundingClientRect().left"
        )
        assert identity.evaluate(
            "element => getComputedStyle(element).display === 'flex'"
        )
        package_id = card.get_by_text(long_package, exact=True)
        expect(package_id).to_be_visible()
        assert package_id.evaluate(
            "element => element.scrollWidth <= element.clientWidth"
        )
        identity_bounds = identity.bounding_box()
        action_bounds = actions.bounding_box()
        status_bounds = card.get_by_text(long_error).bounding_box()
        assert identity_bounds and action_bounds and status_bounds
        assert identity_bounds["y"] + identity_bounds["height"] <= action_bounds["y"]
        assert action_bounds["y"] + action_bounds["height"] <= status_bounds["y"]
        error = card.get_by_text(long_error)
        expect(error).to_be_visible()
        assert error.evaluate("element => element.scrollWidth <= element.clientWidth")

        selection = card.get_by_role("checkbox", name="Select app for build")
        label = selection.locator("xpath=ancestor::label")
        assert label.evaluate(
            "element => getComputedStyle(element).position !== 'absolute' && element.getBoundingClientRect().height >= 44"
        )
        assert card.evaluate(
            "element => element.getBoundingClientRect().right <= innerWidth"
        )
        assert document_width(local_page) <= width

    checkbox = card.get_by_role("checkbox", name="Select app for build")
    checkbox.focus()
    assert checkbox.evaluate("element => document.activeElement === element")
    local_page.keyboard.press("Space")
    expect(checkbox).to_be_checked()
    expect(card.get_by_text("Selected for build")).to_be_visible()
    local_page.keyboard.press("Space")
    expect(checkbox).not_to_be_checked()

    card.get_by_role("button", name="Edit Configuration").click()
    expect(
        local_page.locator('[role="dialog"][aria-labelledby="patch-dialog-title"]')
    ).to_be_visible()
    local_page.keyboard.press("Escape")
    expect(
        local_page.locator('[role="dialog"][aria-labelledby="patch-dialog-title"]')
    ).to_be_hidden()

    card.get_by_role("button", name="Upload Manual APK").click()
    expect(
        local_page.locator('[role="dialog"][aria-labelledby="upload-dialog-title"]')
    ).to_be_visible()
    local_page.keyboard.press("Escape")
    expect(
        local_page.locator('[role="dialog"][aria-labelledby="upload-dialog-title"]')
    ).to_be_hidden()

    assert card.get_by_role("link", name="Open Download Page").get_attribute(
        "href"
    ) == (f"/view/{PACKAGE_ID}")
    assert local_page.evaluate("window.__pageErrors") == []


def test_patch_dialog_contains_focus_and_escape_restores_its_opener(
    local_page: Page,
):
    opener = local_page.locator('[title="Edit Configuration"]')
    opener.focus()
    opener.click()
    dialog = local_page.locator('[role="dialog"][aria-labelledby="patch-dialog-title"]')

    expect(dialog).to_be_visible()
    expect(local_page.locator('[role="dialog"]:visible')).to_have_count(1)
    expect(local_page.locator(".glass-surface:visible")).to_have_count(2)
    assert dialog.locator("h2").inner_text() == "Edit Configuration"
    assert local_page.evaluate("document.activeElement.matches('[role=dialog]')")
    local_page.keyboard.press("Shift+Tab")
    assert local_page.evaluate(
        "document.activeElement.textContent.includes('Save Configuration')"
    )
    local_page.keyboard.press("Tab")
    assert local_page.evaluate(
        "document.activeElement.getAttribute('aria-label') === 'Close patch dialog'"
    )
    local_page.keyboard.press("Shift+Tab")
    assert local_page.evaluate(
        "document.activeElement.textContent.includes('Save Configuration')"
    )
    local_page.keyboard.press("Escape")

    expect(dialog).to_be_hidden()
    assert opener.evaluate("element => document.activeElement === element")
    assert local_page.evaluate("window.__pageErrors") == []


def test_upload_dialog_contains_focus_and_escape_restores_its_opener(
    local_page: Page,
):
    opener = local_page.locator('[title="Upload Manual APK"]')
    opener.focus()
    opener.click()
    dialog = local_page.locator(
        '[role="dialog"][aria-labelledby="upload-dialog-title"]'
    )

    expect(dialog).to_be_visible()
    expect(local_page.locator('[role="dialog"]:visible')).to_have_count(1)
    expect(local_page.locator(".glass-surface:visible")).to_have_count(2)
    assert dialog.locator("h2").inner_text() == "Upload Manual APK"
    assert local_page.evaluate("document.activeElement.matches('[role=dialog]')")
    local_page.keyboard.press("Shift+Tab")
    assert local_page.evaluate(
        "document.activeElement.textContent.includes('Upload APK')"
    )
    local_page.keyboard.press("Tab")
    assert local_page.evaluate(
        "document.activeElement.getAttribute('aria-label') === 'Close upload dialog'"
    )
    local_page.keyboard.press("Escape")

    expect(dialog).to_be_hidden()
    assert opener.evaluate("element => document.activeElement === element")
    assert local_page.evaluate("window.__pageErrors") == []


def test_upload_failures_keep_file_and_version_for_retry(local_page: Page):
    outcomes: list[tuple[int, dict[str, str]] | None] = [
        (400, {"detail": "Rejected <img src=x onerror=alert(1)>"}),
        (403, {"detail": "Upload refused by the server."}),
        None,
        (200, {"message": "Uploaded fixture.apk as org.example.fixture-1.2.3.apk"}),
    ]

    def respond_to_upload(route):
        outcome = outcomes.pop(0)
        if outcome is None:
            route.abort()
            return
        status, payload = outcome
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(payload),
        )

    local_page.route("**/api/ui/upload_apk", respond_to_upload)
    dialogs: list[str] = []

    def dismiss_alert(dialog) -> None:
        dialogs.append(dialog.message)
        dialog.dismiss()

    local_page.on("dialog", dismiss_alert)
    opener = local_page.locator('[title="Upload Manual APK"]')
    opener.click()
    dialog = local_page.locator(
        '[role="dialog"][aria-labelledby="upload-dialog-title"]'
    )
    expect(dialog).to_be_visible()
    version = dialog.locator('input[type="text"]')
    file_input = dialog.locator('input[type="file"]')
    version.fill("1.2.3")
    file_input.set_input_files(
        {
            "name": "fixture.apk",
            "mimeType": "application/vnd.android.package-archive",
            "buffer": b"fixture apk data",
        }
    )

    expected_errors = [
        "Rejected <img src=x onerror=alert(1)>",
        "Upload refused by the server.",
        "The server could not be reached. Check your connection and try again.",
    ]
    for expected_error in expected_errors:
        dialog.get_by_role("button", name="Upload APK").click()
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role("alert")).to_have_text(expected_error)
        assert version.input_value() == "1.2.3"
        assert file_input.evaluate("element => element.files[0]?.name") == "fixture.apk"
        assert dialog.get_by_role("alert").locator("img").count() == 0
        assert dialogs == []

    dialog.get_by_role("button", name="Upload APK").click()
    expect(dialog).to_be_hidden()
    assert local_page.evaluate("Alpine.$data(document.body).uploadError === null")
    assert dialogs == ["Uploaded fixture.apk as org.example.fixture-1.2.3.apk"]
    assert local_page.evaluate("window.__pageErrors") == []


def test_build_refusal_and_network_failure_restore_polled_state(local_page: Page):
    outcomes = [(403, b'{"detail":"Build refused"}'), (None, b"")]
    local_page.route(
        "**/api/ui/trigger_build",
        lambda route: (
            route.abort()
            if (outcome := outcomes.pop(0))[0] is None
            else route.fulfill(
                status=outcome[0],
                content_type="application/json",
                body=outcome[1],
            )
        ),
    )
    build_button = local_page.get_by_role("button", name="Build All Apps")
    for expected in ("Build refused", "Could not reach the server."):
        build_button.click()
        expect(local_page.get_by_role("alert")).to_have_text(expected)
        assert build_button.is_enabled()
        assert local_page.evaluate("Alpine.$data(document.body).building") is False


def test_patch_save_refusal_keeps_dialog_and_selection(local_page: Page):
    outcomes = [(400, "invalid error body"), (None, "")]

    def respond(route):
        status, body = outcomes.pop(0)
        if status is None:
            route.abort()
        else:
            route.fulfill(status=status, content_type="text/plain", body=body)

    local_page.route(
        "**/api/ui/add_app",
        respond,
    )
    local_page.locator('[title="Edit Configuration"]').click()
    dialog = local_page.locator('[role="dialog"][aria-labelledby="patch-dialog-title"]')
    expect(dialog).to_be_visible()
    patch_checkbox = dialog.get_by_role("checkbox").first
    expect(patch_checkbox).to_be_visible()
    patch_checkbox.check()
    dialog.get_by_role("button", name="Save Configuration").click()

    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("alert")).to_have_text("HTTP 400")
    assert dialog.get_by_role("checkbox").is_checked()
    dialog.get_by_role("button", name="Save Configuration").click()
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("alert")).to_have_text("Could not reach the server.")
    assert dialog.get_by_role("checkbox").is_checked()


def test_add_source_refusal_shows_inline_error_and_keeps_repo_for_retry(
    local_page: Page,
):
    outcomes = [(403, "invalid error body"), (None, "")]

    def respond(route):
        status, body = outcomes.pop(0)
        if status is None:
            route.abort()
        else:
            route.fulfill(status=status, content_type="text/plain", body=body)

    local_page.route(
        "**/api/ui/add_source",
        respond,
    )
    local_page.evaluate("Alpine.$data(document.body).activeTab = 'discovery'")
    local_page.evaluate(
        "window.promptDefaults=[]; window.prompt=(_message, value='') => "
        "(window.promptDefaults.push(value), 'FixtureOrg/new-source')"
    )
    local_page.get_by_role("button", name="+ Add Custom Source").click()

    expect(local_page.get_by_role("alert")).to_have_text("HTTP 403")
    assert local_page.evaluate("window.promptDefaults")[0] == ""
    local_page.get_by_role("button", name="+ Add Custom Source").click()
    expect(local_page.get_by_role("alert")).to_have_text("Could not reach the server.")
    assert local_page.evaluate("window.promptDefaults")[-1] == "FixtureOrg/new-source"


def test_source_toggle_and_delete_refusals_preserve_source_state(local_page: Page):
    outcomes = [
        (400, b'{"detail":"Toggle refused"}'),
        (None, b""),
        (403, b"not json"),
        (None, b""),
    ]

    def respond(route):
        status, body = outcomes.pop(0)
        if status is None:
            route.abort()
        else:
            route.fulfill(status=status, content_type="application/json", body=body)

    local_page.route("**/api/ui/toggle_source", respond)
    local_page.route("**/api/ui/delete_source", respond)
    local_page.evaluate("Alpine.$data(document.body).activeTab = 'discovery'")
    source = local_page.get_by_text("Fixture Source", exact=True).locator(
        "xpath=../../.."
    )
    local_page.once("dialog", lambda dialog: dialog.accept())
    active = source.get_by_role("button", name="Active")
    active.click()
    expect(source.get_by_role("button", name="Active")).to_be_visible()
    expect(source.get_by_role("alert")).to_have_text("Toggle refused")
    active.click()
    expect(source.get_by_role("button", name="Active")).to_be_visible()
    expect(source.get_by_role("alert")).to_have_text("Could not reach the server.")

    local_page.once("dialog", lambda dialog: dialog.accept())
    source.get_by_role("button").nth(1).click()
    expect(source).to_be_visible()
    expect(source.get_by_role("alert")).to_have_text("HTTP 403")
    local_page.once("dialog", lambda dialog: dialog.accept())
    source.get_by_role("button").nth(1).click()
    expect(source).to_be_visible()
    expect(source.get_by_role("alert")).to_have_text("Could not reach the server.")


def test_app_delete_refusal_keeps_card_and_confirmation(local_page: Page):
    outcomes = [
        (403, '{"detail":"Rejected <img src=x onerror=alert(1)>"}'),
        (None, ""),
    ]

    def respond(route):
        status, body = outcomes.pop(0)
        if status is None:
            route.abort()
        else:
            route.fulfill(status=status, content_type="application/json", body=body)

    local_page.route(
        "**/api/ui/delete_app",
        respond,
    )
    navigations = []
    local_page.on("framenavigated", lambda frame: navigations.append(frame.url))
    local_page.once("dialog", lambda dialog: dialog.accept())
    local_page.locator('[title="Remove App"]').click()

    expect(local_page.get_by_text("Fixture App", exact=True)).to_be_visible()
    expect(local_page.get_by_role("alert")).to_have_text(
        "Rejected <img src=x onerror=alert(1)>"
    )
    assert local_page.get_by_role("alert").locator("img").count() == 0
    for width in (320, 390, 768, 1280):
        local_page.set_viewport_size({"width": width, "height": 844})
        assert document_width(local_page) <= width
    assert navigations == []
    local_page.once("dialog", lambda dialog: dialog.accept())
    local_page.locator('[title="Remove App"]').click()
    expect(local_page.get_by_text("Fixture App", exact=True)).to_be_visible()
    expect(local_page.get_by_role("alert")).to_have_text("Could not reach the server.")
    assert navigations == []


def test_notification_body_error_is_inline_even_on_success_http_status(
    local_page: Page,
):
    outcomes = [
        (200, '{"status":"error","message":"No notification was sent."}'),
        (403, "not json"),
        (None, ""),
        (200, '{"status":"success"}'),
    ]

    def respond(route):
        status, body = outcomes.pop(0)
        if status is None:
            route.abort()
        else:
            route.fulfill(status=status, content_type="application/json", body=body)

    local_page.route(
        "**/api/ui/test_notification",
        respond,
    )
    local_page.route(
        "**/api/ui/secrets",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "github-token": {"set": False, "source": None},
                    "apprise-urls": {
                        "set": True,
                        "count": 1,
                        "source": "file",
                        "legacy_available": False,
                        "migration_state": "none",
                    },
                }
            ),
        ),
    )
    local_page.get_by_role("button", name="Settings").click()
    expect(local_page.get_by_role("button", name="Test Notifications")).to_be_enabled()
    local_page.get_by_role("button", name="Test Notifications").click()

    expect(local_page.get_by_role("alert")).to_have_text(
        "Failed to send test notification."
    )
    local_page.get_by_role("button", name="Test Notifications").click()
    expect(local_page.get_by_role("alert")).to_have_text("HTTP 403")
    local_page.get_by_role("button", name="Test Notifications").click()
    expect(local_page.get_by_role("alert")).to_have_text("Could not reach the server.")
    local_page.get_by_role("button", name="Test Notifications").click()
    expect(local_page.get_by_role("alert")).to_have_text("Test notification sent.")


def test_apprise_destination_is_masked_write_only_and_responsive(local_page: Page):
    stored_status = {
        "github-token": {"set": False, "source": None},
        "apprise-urls": {
            "set": True,
            "count": 1,
            "source": "file",
            "legacy_available": False,
            "migration_state": "none",
        },
    }
    submitted = []

    def save_destination(route) -> None:
        submitted.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(stored_status),
        )

    local_page.route("**/api/ui/secrets/apprise-urls", save_destination)
    local_page.set_viewport_size({"width": 320, "height": 844})
    local_page.get_by_role("button", name="Settings").click()
    destination = local_page.get_by_role("textbox", name="Notification destination URL")
    expect(destination).to_have_attribute("type", "password")
    destination.fill("json://fixture-user:fixture-password@localhost")
    destination.press("Enter")
    expect(local_page.get_by_text("Destination 1")).to_be_visible()
    assert "fixture-password" not in local_page.locator("body").inner_text()
    save = local_page.get_by_role("button", name="Save destinations")
    save.focus()
    save.press("Enter")

    expect(local_page.get_by_role("alert")).to_have_text(
        "Saved 1 notification destinations. The previous list was replaced."
    )
    assert submitted == [{"value": ["json://fixture-user:fixture-password@localhost"]}]
    assert document_width(local_page) <= 320


def test_build_already_running_response_uses_polled_status(local_page: Page):
    local_page.route(
        "**/api/ui/trigger_build",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"status":"Build already in progress"}',
        ),
    )
    local_page.route(
        "**/api/ui/build_status",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"in_progress":true,"message":"Building","last_run":"Now"}',
        ),
    )
    local_page.get_by_role("button", name="Build All Apps").click()

    expect(local_page.get_by_role("button", name="Building")).to_be_disabled()
    assert local_page.evaluate("Alpine.$data(document.body).buildError") is None


def test_source_success_updates_and_removes_only_after_acceptance(local_page: Page):
    local_page.route(
        "**/api/ui/toggle_source",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"status":"success"}'
        ),
    )
    local_page.route(
        "**/api/ui/delete_source",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"status":"success"}'
        ),
    )
    local_page.evaluate("Alpine.$data(document.body).activeTab = 'discovery'")
    source = local_page.get_by_text("Fixture Source", exact=True).locator(
        "xpath=../../.."
    )
    source.get_by_role("button", name="Active").click()
    expect(source.get_by_role("button", name="Disabled")).to_be_visible()
    local_page.once("dialog", lambda dialog: dialog.accept())
    source.get_by_role("button").nth(1).click()
    expect(source).to_have_count(0)


def test_patch_save_and_app_delete_success_keep_reload_behavior(local_page: Page):
    local_page.route(
        "**/api/ui/add_app",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"status":"success"}'
        ),
    )
    local_page.locator('[title="Edit Configuration"]').click()
    dialog = local_page.locator('[role="dialog"][aria-labelledby="patch-dialog-title"]')
    with local_page.expect_navigation():
        dialog.get_by_role("button", name="Save Configuration").click()

    local_page.route(
        "**/api/ui/delete_app",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"status":"success"}'
        ),
    )
    local_page.once("dialog", lambda prompt: prompt.accept())
    with local_page.expect_navigation():
        local_page.locator('[title="Remove App"]').click()


def test_add_source_success_keeps_reload_behavior(local_page: Page):
    local_page.route(
        "**/api/ui/add_source",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"status":"success"}'
        ),
    )
    local_page.evaluate("Alpine.$data(document.body).activeTab = 'discovery'")
    local_page.evaluate("window.prompt=() => 'FixtureOrg/new-source'")
    with local_page.expect_navigation():
        local_page.get_by_role("button", name="+ Add Custom Source").click()
