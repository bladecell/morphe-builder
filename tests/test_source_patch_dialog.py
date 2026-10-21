"""Browser tests for source-aware patch selection in the admin dialog."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Error, Page, expect, sync_playwright

from src import main

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ID = "org.example.patchfixture"


def grouped_catalog() -> dict:
    first, second = "one/patches", "two/patches"
    inactive, deleted, stale, unreadable = (
        "inactive/patches",
        "deleted/patches",
        "stale/patches",
        "unreadable/patches",
    )
    return {
        "sources": [
            {
                "repo": first,
                "source": "Shared Name",
                "is_base": False,
                "status": "available",
                "patches": [
                    {
                        "name": "Same",
                        "description": "First provider",
                        "enabled_by_default": True,
                        "source": "Shared Name",
                        "is_base": False,
                        "selected": True,
                    }
                ],
            },
            {
                "repo": second,
                "source": "Shared Name",
                "is_base": False,
                "status": "available",
                "patches": [
                    {
                        "name": "Same",
                        "description": "Second provider",
                        "enabled_by_default": True,
                        "source": "Shared Name",
                        "is_base": False,
                        "selected": True,
                    }
                ],
            },
            {
                "repo": inactive,
                "source": "Inactive Source",
                "is_base": False,
                "status": "inactive",
                "patches": [
                    {
                        "name": "Keep inactive",
                        "description": "Stored selection",
                        "enabled_by_default": False,
                        "source": "Inactive Source",
                        "is_base": False,
                        "selected": True,
                    }
                ],
            },
            {
                "repo": deleted,
                "source": deleted,
                "is_base": False,
                "status": "deleted",
                "patches": [
                    {
                        "name": "Keep deleted",
                        "description": "Stored selection",
                        "enabled_by_default": False,
                        "source": deleted,
                        "is_base": False,
                        "selected": True,
                    }
                ],
            },
            {
                "repo": stale,
                "source": "Stale Source",
                "is_base": False,
                "status": "available",
                "patches": [
                    {
                        "name": "Removed upstream",
                        "description": "Stored selection",
                        "enabled_by_default": False,
                        "source": "Stale Source",
                        "is_base": False,
                        "selected": True,
                        "status": "available",
                    }
                ],
            },
            {
                "repo": unreadable,
                "source": "Unreadable Source",
                "is_base": False,
                "status": "unreadable",
                "patches": [],
            },
        ],
        "selection": {
            "stored": {
                first: ["Same"],
                second: ["Same"],
                inactive: ["Keep inactive"],
                deleted: ["Keep deleted"],
                stale: ["Removed upstream"],
            },
            "resolved": {first: ["Same"], second: ["Same"]},
            "status": "partial",
            "unresolved": [
                {
                    "repo": inactive,
                    "name": "Keep inactive",
                    "reason": "source_unavailable",
                },
                {
                    "repo": deleted,
                    "name": "Keep deleted",
                    "reason": "source_unavailable",
                },
                {
                    "repo": stale,
                    "name": "Removed upstream",
                    "reason": "patch_unavailable",
                },
            ],
            "error": {
                "code": "selection_partial",
                "message": "Some selected patches are unavailable; review the selection.",
            },
        },
    }


def second_app_catalog() -> dict:
    return {
        "sources": [
            {
                "repo": "second/repo",
                "source": "Second Source",
                "is_base": False,
                "status": "available",
                "patches": [
                    {
                        "name": "Second Patch",
                        "description": "For the second app",
                        "enabled_by_default": False,
                        "source": "Second Source",
                        "is_base": False,
                        "selected": False,
                    }
                ],
            }
        ],
        "selection": {
            "stored": {},
            "resolved": {},
            "status": "empty",
            "unresolved": [],
            "error": None,
        },
    }


@pytest.fixture
def patch_dialog_page():
    state = {
        "catalog": grouped_catalog(),
        "catalog_status": 200,
        "post_status": 200,
        "posts": [],
    }
    template = main.templates.get_template("index.html")

    def render() -> bytes:
        return template.render(
            config={
                "apps": [
                    {
                        "id": PACKAGE_ID,
                        "name": "Patch Fixture",
                        "include_patches": state["catalog"]["selection"]["stored"],
                    },
                    {
                        "id": "org.example.secondfixture",
                        "name": "Second Fixture",
                        "include_patches": {},
                    },
                ],
                "sources": [],
                "settings": {"server_url": "", "cron_schedule": ""},
            },
            available_apps=[{"pkg": PACKAGE_ID, "name": "Patch Fixture"}],
            metadata={},
            request=None,
        ).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                self.respond(200, "text/html", render())
                return
            if path.startswith("/static/"):
                asset = (ROOT / "src" / path.lstrip("/")).resolve()
                if asset.is_relative_to(ROOT / "src" / "static") and asset.is_file():
                    content_type = (
                        "text/css" if asset.suffix == ".css" else "text/javascript"
                    )
                    self.respond(200, content_type, asset.read_bytes())
                    return
            if path == f"/api/ui/patches/{PACKAGE_ID}":
                self.respond(
                    state["catalog_status"],
                    "application/json",
                    json.dumps(
                        state["catalog"]
                        if state["catalog_status"] == 200
                        else {"detail": "Patch catalog could not be read."}
                    ).encode(),
                )
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
                "/api/ui/updates": {},
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
                "/api/ui/logs": {"logs": ""},
                "/api/ui/metadata": {},
            }
            if path in payloads:
                self.respond(
                    200, "application/json", json.dumps(payloads[path]).encode()
                )
                return
            self.respond(404, "application/json", b"{}")

        def do_POST(self):
            if urlsplit(self.path).path != "/api/ui/add_app":
                self.respond(404, "application/json", b"{}")
                return
            length = int(self.headers.get("Content-Length", "0"))
            state["posts"].append(json.loads(self.rfile.read(length)))
            status = state["post_status"]
            body = (
                b'{"detail":"Selection was rejected."}'
                if status != 200
                else b'{"status":"success"}'
            )
            self.respond(status, "application/json", body)

        def respond(self, status: int, content_type: str, body: bytes):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
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
                "window.__pageErrors=[]; window.addEventListener('error', e => window.__pageErrors.push(e.message));"
            )
            page.add_init_script(
                """(() => {
                    const nativeFetch = window.fetch.bind(window);
                    window.__pendingCatalogRequests = [];
                    window.fetch = (input, init) => {
                        const url = String(input);
                        if (!window.__deferCatalogRequests || !url.includes('/api/ui/patches/')) return nativeFetch(input, init);
                        return new Promise(resolve => window.__pendingCatalogRequests.push({
                            url,
                            resolveCatalog(body, status = 200) {
                                resolve(new Response(JSON.stringify(body), {
                                    status,
                                    headers: {'Content-Type': 'application/json'}
                                }));
                            }
                        }));
                    };
                })();"""
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
            yield page, state
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def open_edit(page: Page):
    page.locator('[title="Edit Configuration"]').first.click()
    dialog = page.locator('[role="dialog"][aria-labelledby="patch-dialog-title"]')
    expect(dialog).to_be_visible()
    return dialog


def test_grouped_dialog_uses_repo_identity_and_keeps_duplicate_names_independent(
    patch_dialog_page,
):
    page, _ = patch_dialog_page
    dialog = open_edit(page)
    tabs = dialog.get_by_role("tab")
    expect(tabs).to_have_count(5)
    shared = tabs.filter(has_text="Shared Name")
    expect(shared).to_have_count(2)
    assert shared.nth(0).get_attribute("data-repo") == "one/patches"
    assert shared.nth(1).get_attribute("data-repo") == "two/patches"
    first_tab, second_tab = shared.nth(0), shared.nth(1)
    first_tab.click()
    first_checkbox = dialog.get_by_role("checkbox", name="Select Same from one/patches")
    expect(first_checkbox).to_be_checked()
    second_tab.click()
    second_checkbox = dialog.get_by_role(
        "checkbox", name="Select Same from two/patches"
    )
    expect(second_checkbox).to_be_checked()
    second_checkbox.uncheck()
    first_tab.click()
    expect(
        dialog.get_by_role("checkbox", name="Select Same from one/patches")
    ).to_be_checked()


def test_selected_source_opens_first_and_empty_sources_are_disclosed(
    patch_dialog_page,
):
    page, state = patch_dialog_page
    state["catalog"]["sources"][0]["patches"][0]["selected"] = False
    state["catalog"]["selection"]["stored"] = {"two/patches": ["Same"]}
    state["catalog"]["selection"]["resolved"] = {"two/patches": ["Same"]}

    dialog = open_edit(page)
    selected_tab = dialog.get_by_role("tab").filter(has_text="two/patches")
    expect(selected_tab).to_have_attribute("aria-selected", "true")
    expect(selected_tab).to_contain_text("1 available")
    expect(selected_tab).to_contain_text("Selected")

    disclosure = dialog.locator("details summary")
    expect(disclosure).to_be_visible()
    disclosure.focus()
    page.keyboard.press("Enter")
    page.keyboard.press("Tab")
    empty_tab = dialog.get_by_role("tab").filter(has_text="unreadable/patches")
    expect(empty_tab).to_be_visible()
    expect(empty_tab).to_have_attribute("tabindex", "0")
    expect(empty_tab).to_be_focused()
    empty_tab.click()
    expect(dialog.get_by_role("status").filter(has_text="unreadable")).to_be_visible()


def test_active_source_stays_visible_when_last_saved_patch_is_removed(
    patch_dialog_page,
):
    page, _ = patch_dialog_page
    dialog = open_edit(page)
    stale_tab = dialog.get_by_role("tab").filter(has_text="stale/patches")
    stale_tab.click()
    dialog.get_by_role("checkbox", name="Removed upstream").uncheck()

    expect(stale_tab).to_be_visible()
    expect(stale_tab).to_have_attribute("aria-selected", "true")
    expect(
        dialog.get_by_role("tabpanel").filter(has_text="Removed upstream")
    ).to_be_visible()


def test_all_empty_catalog_keeps_initial_source_tab_discoverable(patch_dialog_page):
    page, state = patch_dialog_page
    for source in state["catalog"]["sources"]:
        source["patches"] = []
    state["catalog"]["selection"] = {
        "stored": {},
        "resolved": {},
        "status": "empty",
        "unresolved": [],
        "error": None,
    }

    dialog = open_edit(page)
    first_tab = dialog.get_by_role("tab").filter(has_text="one/patches")
    expect(first_tab).to_be_visible()
    expect(first_tab).to_have_attribute("aria-selected", "true")
    expect(dialog.get_by_role("tabpanel")).to_be_visible()


def test_newer_app_catalog_request_owns_loading_selection_and_save_payload(
    patch_dialog_page,
):
    page, state = patch_dialog_page
    page.evaluate("window.__deferCatalogRequests = true")
    edit_buttons = page.locator('[title="Edit Configuration"]')
    edit_buttons.nth(0).click()
    page.wait_for_function("window.__pendingCatalogRequests.length === 1")
    page.keyboard.press("Escape")
    edit_buttons.nth(1).click()
    page.wait_for_function("window.__pendingCatalogRequests.length === 2")

    page.evaluate(
        "body => window.__pendingCatalogRequests[1].resolveCatalog(body)",
        second_app_catalog(),
    )
    dialog = page.locator('[role="dialog"][aria-labelledby="patch-dialog-title"]')
    expect(
        dialog.get_by_role("checkbox", name="Select Second Patch from second/repo")
    ).to_be_visible()

    page.evaluate(
        "body => window.__pendingCatalogRequests[0].resolveCatalog(body)",
        grouped_catalog(),
    )
    expect(
        dialog.get_by_role("checkbox", name="Select Second Patch from second/repo")
    ).to_be_visible()
    expect(dialog.get_by_role("tab")).to_have_count(1)
    expect(dialog.get_by_role("button", name="Save Configuration")).to_be_enabled()
    dialog.get_by_role("checkbox", name="Select Second Patch from second/repo").check()
    dialog.get_by_role("button", name="Save Configuration").click()
    page.wait_for_timeout(100)

    assert state["posts"] == [
        {
            "package_id": "org.example.secondfixture",
            "name": "Second Fixture",
            "include_patches": {"second/repo": ["Second Patch"]},
        }
    ]


def test_closed_dialog_stale_catalog_error_cannot_change_reopened_dialog(
    patch_dialog_page,
):
    page, _ = patch_dialog_page
    page.evaluate("window.__deferCatalogRequests = true")
    edit_buttons = page.locator('[title="Edit Configuration"]')
    edit_buttons.nth(0).click()
    page.wait_for_function("window.__pendingCatalogRequests.length === 1")
    page.keyboard.press("Escape")
    edit_buttons.nth(1).click()
    page.wait_for_function("window.__pendingCatalogRequests.length === 2")
    dialog = page.locator('[role="dialog"][aria-labelledby="patch-dialog-title"]')

    page.evaluate(
        "() => window.__pendingCatalogRequests[0].resolveCatalog({detail: 'Old request failed.'}, 502)"
    )
    expect(
        dialog.get_by_role("status").filter(has_text="Loading patch sources")
    ).to_be_visible()
    expect(dialog.get_by_role("alert")).to_have_count(0)

    page.evaluate(
        "body => window.__pendingCatalogRequests[1].resolveCatalog(body)",
        second_app_catalog(),
    )
    expect(
        dialog.get_by_role("checkbox", name="Select Second Patch from second/repo")
    ).to_be_visible()


def test_edit_shows_backend_resolved_legacy_choice_without_saving_until_explicit_submit(
    patch_dialog_page,
):
    page, state = patch_dialog_page
    first = "one/patches"
    state["catalog"]["selection"] = {
        "stored": ["Same", "Unmatched legacy"],
        "resolved": {first: ["Same"]},
        "status": "partial",
        "unresolved": [
            {"repo": None, "name": "Unmatched legacy", "reason": "patch_unavailable"}
        ],
        "error": {
            "code": "selection_partial",
            "message": "Review unresolved selection.",
        },
    }
    state["catalog"]["sources"][0]["patches"][0]["selected"] = False
    state["catalog"]["sources"][1]["patches"][0]["selected"] = False
    for source in state["catalog"]["sources"][2:]:
        for patch in source["patches"]:
            patch["selected"] = False
    dialog = open_edit(page)
    page.wait_for_timeout(100)
    tabs = dialog.get_by_role("tab")
    tabs.filter(has_text="one/patches").click()
    expect(
        dialog.get_by_role("checkbox", name="Select Same from one/patches")
    ).to_be_checked()
    expect(dialog.get_by_text("Unmatched legacy")).to_be_visible()
    assert state["posts"] == []
    dialog.get_by_role("button", name="Remove unresolved selection").click()
    dialog.get_by_role("button", name="Save Configuration").click()
    page.wait_for_timeout(100)
    assert state["posts"] == [
        {
            "package_id": PACKAGE_ID,
            "name": "Patch Fixture",
            "include_patches": {first: ["Same"]},
        }
    ]


def test_unavailable_selections_roundtrip_unless_explicitly_removed(patch_dialog_page):
    page, state = patch_dialog_page
    dialog = open_edit(page)
    for repo, patch_name in [
        ("inactive/patches", "Keep inactive"),
        ("deleted/patches", "Keep deleted"),
        ("stale/patches", "Removed upstream"),
    ]:
        tab = dialog.get_by_role("tab").filter(has_text=repo)
        tab.click()
        expect(
            dialog.get_by_role("checkbox", name=f"Select {patch_name} from {repo}")
        ).to_be_checked()
    dialog.get_by_role("tab").filter(has_text="deleted/patches").click()
    dialog.get_by_role("checkbox", name="Keep deleted").uncheck()
    dialog.get_by_role("button", name="Save Configuration").click()
    page.wait_for_timeout(100)
    assert state["posts"][0]["include_patches"] == {
        "one/patches": ["Same"],
        "two/patches": ["Same"],
        "inactive/patches": ["Keep inactive"],
        "stale/patches": ["Removed upstream"],
    }


def test_failed_save_keeps_selection_and_surfaces_inline_error(patch_dialog_page):
    page, state = patch_dialog_page
    state["post_status"] = 400
    dialog = open_edit(page)
    dialog.get_by_role("button", name="Save Configuration").click()
    expect(dialog.get_by_role("alert")).to_have_text("Selection was rejected.")
    expect(dialog).to_be_visible()
    dialog.get_by_role("tab").filter(has_text="deleted/patches").click()
    expect(
        dialog.get_by_role("checkbox", name="Select Keep deleted from deleted/patches")
    ).to_be_checked()
    assert page.evaluate("window.__pageErrors") == []


def test_grouped_catalog_error_is_inline_and_never_rendered_as_patch_data(
    patch_dialog_page,
):
    page, state = patch_dialog_page
    state["catalog_status"] = 502
    dialog = open_edit(page)

    expect(dialog.get_by_role("alert")).to_have_text("Patch catalog could not be read.")
    expect(dialog.get_by_role("tab")).to_have_count(0)
    expect(dialog.get_by_text("Patch catalog could not be read.")).to_have_count(1)
    assert page.evaluate("window.__pageErrors") == []


def test_partial_grouped_selection_keeps_healthy_duplicate_provider_choices(
    patch_dialog_page,
):
    page, state = patch_dialog_page
    catalog = state["catalog"]
    catalog["sources"] = catalog["sources"][:2] + [catalog["sources"][-1]]
    catalog["selection"] = {
        "stored": {
            "one/patches": ["Same"],
            "two/patches": ["Same"],
            "bad/patches": ["Old"],
        },
        "resolved": {"one/patches": ["Same"], "two/patches": ["Same"]},
        "status": "partial",
        "unresolved": [
            {"repo": "bad/patches", "name": "Old", "reason": "source_unavailable"}
        ],
        "error": {
            "code": "selection_partial",
            "message": "Some selected patches are unavailable; review the selection.",
        },
    }
    dialog = open_edit(page)

    expect(dialog.get_by_role("alert")).to_have_count(0)
    expect(
        dialog.get_by_text(
            "Some selected patches are unavailable; review the selection."
        )
    ).to_be_visible()
    expect(
        dialog.get_by_role("checkbox", name="Select Same from one/patches")
    ).to_be_checked()
    dialog.get_by_role("tab").filter(has_text="two/patches").click()
    expect(
        dialog.get_by_role("checkbox", name="Select Same from two/patches")
    ).to_be_checked()
    assert page.evaluate("window.__pageErrors") == []


def test_unreadable_source_warning_does_not_block_other_sources(patch_dialog_page):
    page, _ = patch_dialog_page
    dialog = open_edit(page)
    dialog.locator("details summary").click()
    dialog.get_by_role("tab").filter(has_text="unreadable/patches").click()
    expect(dialog.get_by_role("status").filter(has_text="unreadable")).to_be_visible()
    dialog.get_by_role("tab").filter(has_text="one/patches").click()
    expect(
        dialog.get_by_role("checkbox", name="Select Same from one/patches")
    ).to_be_enabled()


def test_new_app_uses_defaults_from_each_available_source(patch_dialog_page):
    page, state = patch_dialog_page
    state["catalog"]["selection"] = {
        "stored": [],
        "resolved": {},
        "status": "empty",
        "unresolved": [],
        "error": None,
    }
    for source in state["catalog"]["sources"]:
        for patch in source["patches"]:
            patch["selected"] = False
    state["catalog"]["sources"][0]["patches"][0]["enabled_by_default"] = True
    state["catalog"]["sources"][1]["patches"][0]["enabled_by_default"] = True
    page.get_by_role("button", name="Discovery").click()
    page.get_by_role("button", name="Configure").click()
    dialog = page.locator('[role="dialog"][aria-labelledby="patch-dialog-title"]')
    expect(dialog).to_be_visible()
    expect(
        dialog.get_by_role("checkbox", name="Select Same from one/patches")
    ).to_be_checked()
    dialog.get_by_role("tab").filter(has_text="two/patches").click()
    expect(
        dialog.get_by_role("checkbox", name="Select Same from two/patches")
    ).to_be_checked()
    assert state["posts"] == []


def test_source_tabs_are_keyboard_operable_and_reflow_at_phone_width(patch_dialog_page):
    page, _ = patch_dialog_page
    page.set_viewport_size({"width": 320, "height": 844})
    dialog = open_edit(page)
    tabs = dialog.get_by_role("tab")
    tabs.first.focus()
    page.keyboard.press("ArrowRight")
    expect(tabs.nth(1)).to_have_attribute("aria-selected", "true")
    expect(tabs.nth(1)).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
