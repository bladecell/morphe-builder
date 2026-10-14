"""Self-hosted stylesheet and Alpine for the admin page.

The page must load nothing from a third party: the Tailwind stylesheet is built
ahead of time by `scripts/build_css.py` and committed, Alpine is vendored, and the
app serves both under `/static`.
"""

import asyncio
import hashlib
import io
import logging
import re
import shutil
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Self

import pytest
from fastapi.testclient import TestClient

from scripts import build_css
from src import main

ROOT = main.PROJECT_ROOT
STATIC_DIR = ROOT / "src" / "static"
STYLESHEET = STATIC_DIR / "app.css"
ALPINE = STATIC_DIR / "alpine-3.17.4.min.js"
INDEX = ROOT / "src" / "templates" / "index.html"
# SHA-256 of dist/cdn.min.js in the alpinejs@3.17.4 npm tarball. The tarball was
# verified against the registry's dist.integrity, and the same value is jsDelivr's
# published hash for that file (base64 IyUZOUxsj9um82Kx2doWEG21E82/iZAR8A2qtAUd8xw=).
ALPINE_SHA256 = "232519394c6c8fdba6f362b1d9da16106db513cdbf899011f00daab4051df31c"
REBUILD_HINT = "run: uv run python scripts/build_css.py"


@pytest.fixture
def client(data_dir):
    (data_dir / "apks" / "icons").mkdir(parents=True)
    return TestClient(main.app)


class _AssetTags(HTMLParser):
    """Collect `<script src>`, `<link href>` and `<img src>` URLs of a page."""

    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self.links: list[str] = []
        self.preloads: list[str] = []
        self.images: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.scripts.append(values["src"] or "")
        elif tag == "link" and values.get("rel") == "preload":
            self.preloads.append(values["href"] or "")
        elif tag == "link" and values.get("href"):
            self.links.append(values["href"] or "")
        elif tag == "img" and values.get("src"):
            self.images.append(values["src"] or "")


def parse_page(client: TestClient) -> _AssetTags:
    response = client.get("/")
    assert response.status_code == 200
    tags = _AssetTags()
    tags.feed(response.text)
    return tags


# --- the page loads nothing from a third party ---------------------------------------


def test_page_loads_no_script_stylesheet_or_image_from_another_origin(client):
    tags = parse_page(client)

    urls = tags.scripts + tags.links + tags.images
    assert tags.scripts and tags.links
    assert [u for u in urls if re.match(r"(https?:)?//", u)] == []


def test_page_links_the_stylesheet_with_its_content_token_and_alpine_by_version(client):
    tags = parse_page(client)

    assert tags.links == [main.asset_url("app.css")]
    assert tags.scripts == ["/static/alpine-3.17.4.min.js"]
    assert tags.preloads == ["/static/fonts/space-grotesk-latin-700-normal.woff2"]


# --- serving ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/static/app.css", "text/css"),
        ("/static/alpine-3.17.4.min.js", "javascript"),
    ],
)
def test_assets_are_served_with_their_content_type(client, path, content_type):
    response = client.get(path)

    assert response.status_code == 200
    assert content_type in response.headers["content-type"]
    assert response.content == (STATIC_DIR / path.removeprefix("/static/")).read_bytes()


@pytest.mark.parametrize(
    ("host", "extra_headers"),
    [
        ("localhost:8000", {}),
        ("192.168.1.50:8000", {}),
        ("192.168.1.50:8000", {"Sec-Fetch-Site": "same-origin"}),
        ("192.168.1.50:8000", {"Sec-Fetch-Site": "cross-site"}),
    ],
)
def test_static_gets_pass_the_request_guards(client, host, extra_headers):
    for path in ("/static/app.css", "/static/alpine-3.17.4.min.js"):
        response = client.get(path, headers={"Host": host, **extra_headers})

        assert response.status_code == 200, (host, extra_headers, path)


def asgi_get(app, path: str) -> tuple[int, bytes]:
    """GET `path` through an ASGI app exactly as given, with no client-side cleanup.

    httpx collapses `..` before sending, so `TestClient` cannot probe traversal.
    Host is `localhost`, which the request guards accept.
    """
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"localhost")],
        "client": ("127.0.0.1", 1),
        "server": ("localhost", 8000),
    }
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, b"".join(m.get("body", b"") for m in sent)


TRAVERSAL_PATHS = [
    "/static/../templates/index.html",
    "/static/../main.py",
    "/static/../../CLAUDE.md",
    "/static/%2e%2e/templates/index.html",
    "/static/..%2ftemplates/index.html",
    "/static/%2e%2e%2ftemplates%2findex.html",
    "/static/..%2fmain.py",
]


def traversal_target(path: str) -> Path:
    """The file a naive server would read for a traversal path."""
    relative = urllib.parse.unquote(path.removeprefix("/static/"))
    return (STATIC_DIR / relative).resolve()


def test_the_traversal_probe_sees_a_file_that_a_naive_server_would_leak(data_dir):
    async def naive_static(scope, receive, send):
        body = traversal_target(scope["path"]).read_bytes()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})

    status, body = asgi_get(naive_static, "/static/../templates/index.html")

    assert status == 200
    assert body == INDEX.read_bytes()


def test_the_probe_reaches_the_static_mount_for_a_legitimate_file(data_dir):
    status, body = asgi_get(main.app, "/static/app.css")

    assert status == 200
    assert body == STYLESHEET.read_bytes()


@pytest.mark.parametrize("path", TRAVERSAL_PATHS)
def test_static_mount_refuses_path_traversal(data_dir, path):
    status, body = asgi_get(main.app, path)

    assert status == 404
    leaked = traversal_target(path)
    assert not leaked.is_file() or body != leaked.read_bytes()
    assert b"<!DOCTYPE" not in body and b"FastAPI(" not in body


# --- cache busting ------------------------------------------------------------------


def test_stylesheet_url_carries_the_first_twelve_hex_of_the_content_hash():
    token = hashlib.sha256(STYLESHEET.read_bytes()).hexdigest()[:12]

    assert main.asset_url("app.css") == f"/static/app.css?v={token}"


def test_stylesheet_url_changes_when_the_file_bytes_change(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)
    sheet = tmp_path / "app.css"
    sheet.write_bytes(b"a{color:red}")
    first = main.asset_url("app.css")

    sheet.write_bytes(b"a{color:blue}")
    second = main.asset_url("app.css")
    sheet.write_bytes(b"a{color:red}")
    third = main.asset_url("app.css")

    assert first != second
    assert third == first


def test_admin_ui_surfaces_are_flat_and_follow_the_system_theme():
    html = INDEX.read_text()
    stylesheet = STYLESHEET.read_text()

    assert "glass-surface" not in html and "glass-surface" not in stylesheet
    assert "backdrop-filter" not in stylesheet and "backdrop-blur" not in html
    assert "purple" not in html and "pink" not in html
    assert '<html lang="en">' in html
    assert "@media (prefers-color-scheme:dark)" in stylesheet
    assert "@media (prefers-reduced-motion:reduce)" in stylesheet
    assert ":focus-visible{outline:3px" in stylesheet


def test_admin_ui_style_tokens_keep_controls_tappable_and_radii_compact():
    stylesheet = STYLESHEET.read_text()

    assert "--tap-target:44px" in stylesheet
    assert "min-height:var(--tap-target)" in stylesheet
    assert "a.p-2{align-items:center;display:inline-flex;justify-content:center" in stylesheet
    assert "border-radius" not in stylesheet.replace("border-radius:0", "")
    assert "font-size:2.25rem" in stylesheet
    assert "@media (max-width:767px)" in stylesheet
    assert ".max-w-7xl{max-width:80rem}" in stylesheet


def test_templates_render_without_a_request_or_extra_context():
    template = main.templates.get_template("index.html")

    html = template.render(
        config={"apps": [], "sources": []}, available_apps=[], metadata={}, request=None
    )

    assert main.asset_url("app.css") in html


# --- a missing stylesheet must not take the page down -------------------------------


def test_missing_stylesheet_is_logged_and_the_url_is_the_plain_path(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)

    with caplog.at_level(logging.ERROR):
        url = main.asset_url("app.css")

    assert url == "/static/app.css"
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "app.css" in errors[0]
    assert "uv run python scripts/build_css.py" in errors[0]


def test_page_still_renders_when_the_stylesheet_is_missing(
    client, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)

    with caplog.at_level(logging.ERROR):
        first = client.get("/")
        second = client.get("/")

    assert first.status_code == second.status_code == 200
    tags = _AssetTags()
    tags.feed(first.text)
    assert tags.links == ["/static/app.css"]
    assert sum("app.css" in r.getMessage() for r in caplog.records) == 2


# --- vendored Alpine ----------------------------------------------------------------


def test_vendored_alpine_matches_its_pinned_hash():
    assert hashlib.sha256(ALPINE.read_bytes()).hexdigest() == ALPINE_SHA256


# --- staleness of the committed stylesheet ------------------------------------------


def copy_build_inputs(destination: Path) -> None:
    (destination / "src" / "templates").mkdir(parents=True)
    (destination / "tailwind").mkdir()
    for template in (ROOT / "src" / "templates").glob("*.html"):
        shutil.copy(template, destination / "src" / "templates" / template.name)
    for name in ("tailwind.config.js", "input.css"):
        shutil.copy(ROOT / "tailwind" / name, destination / "tailwind" / name)


def test_build_inputs_are_unchanged_since_the_stylesheet_was_built():
    recorded = build_css.HASH_RECORD.read_text().strip()

    assert build_css.inputs_digest(ROOT) == recorded, (
        f"templates, tailwind config or input css changed since app.css was built; "
        f"{REBUILD_HINT}"
    )


@pytest.mark.parametrize(
    "relative",
    [
        "src/templates/index.html",
        "src/templates/obtainium.html",
        "tailwind/tailwind.config.js",
        "tailwind/input.css",
    ],
)
def test_inputs_digest_changes_when_any_scanned_input_changes(tmp_path, relative):
    copy_build_inputs(tmp_path)
    before = build_css.inputs_digest(tmp_path)

    with (tmp_path / relative).open("a") as handle:
        handle.write("\n<!-- changed -->")

    assert build_css.inputs_digest(tmp_path) != before


def test_inputs_digest_ignores_files_the_scanner_does_not_read(tmp_path):
    copy_build_inputs(tmp_path)
    before = build_css.inputs_digest(tmp_path)

    (tmp_path / "src" / "templates" / "notes.txt").write_text("text")
    (tmp_path / "src" / "main.py").write_text("x = 1")

    assert build_css.inputs_digest(tmp_path) == before


def test_committed_stylesheet_equals_a_fresh_build_byte_for_byte(tmp_path):
    try:
        key = build_css.platform_key(*build_css.current_platform())
    except build_css.UnsupportedPlatformError as error:
        pytest.skip(str(error))
    binary = build_css.cached_binary(key, build_css.CACHE_DIR)
    if binary is None:
        pytest.skip(
            "Tailwind binary is not cached (or fails its pinned hash); "
            f"{REBUILD_HINT} once to download it"
        )
    rebuilt = tmp_path / "app.css"

    build_css.build(binary, ROOT, rebuilt)

    assert rebuilt.read_bytes() == STYLESHEET.read_bytes(), (
        f"app.css differs from a fresh build; {REBUILD_HINT}"
    )


# --- the build script ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Linux", "x86_64", "linux-x64"),
        ("Linux", "amd64", "linux-x64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Linux", "arm64", "linux-arm64"),
        ("Darwin", "arm64", "macos-arm64"),
        ("Darwin", "x86_64", "macos-x64"),
    ],
)
def test_platform_key_names_the_pinned_binary(system, machine, expected):
    assert build_css.platform_key(system, machine) == expected
    assert expected in build_css.BINARY_SHA256


@pytest.mark.parametrize(
    ("system", "machine"),
    [("Windows", "AMD64"), ("Linux", "riscv64"), ("FreeBSD", "x86_64")],
)
def test_unsupported_platform_gives_a_clear_error(system, machine):
    with pytest.raises(build_css.UnsupportedPlatformError) as error:
        build_css.platform_key(system, machine)

    assert system in str(error.value) and machine in str(error.value)
    assert "linux-x64" in str(error.value)


def fake_download(payload: bytes, calls: list[str]):
    def download(url: str, destination: Path) -> None:
        calls.append(url)
        destination.write_bytes(payload)

    return download


@pytest.fixture
def no_subprocess(monkeypatch):
    """Fail the test if the script runs any process."""
    launched: list[object] = []

    def refuse(*args, **kwargs):
        launched.append(args)
        raise AssertionError(f"process launched: {args!r}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    return launched


def test_download_with_the_wrong_hash_is_refused_and_never_made_executable(
    tmp_path, no_subprocess
):
    calls: list[str] = []

    with pytest.raises(build_css.BinaryHashError, match="linux-x64"):
        build_css.ensure_binary(
            "linux-x64", tmp_path, fake_download(b"not tailwind", calls)
        )

    assert calls == [
        (
            "https://github.com/tailwindlabs/tailwindcss/releases/download/"
            "v3.4.17/tailwindcss-linux-x64"
        )
    ]
    assert [p for p in tmp_path.iterdir() if p.is_file()] == []
    assert no_subprocess == []


def test_download_with_the_right_hash_is_cached_and_executable(tmp_path, monkeypatch):
    payload = b"#!/bin/sh\necho fake\n"
    monkeypatch.setitem(
        build_css.BINARY_SHA256, "linux-x64", hashlib.sha256(payload).hexdigest()
    )
    calls: list[str] = []

    binary = build_css.ensure_binary(
        "linux-x64", tmp_path, fake_download(payload, calls)
    )
    again = build_css.ensure_binary("linux-x64", tmp_path, fake_download(b"x", calls))

    assert binary == again and binary.read_bytes() == payload
    assert binary.stat().st_mode & 0o111
    assert len(calls) == 1


def test_cached_binary_with_the_wrong_hash_is_refused_not_used(tmp_path, no_subprocess):
    (tmp_path / "tailwindcss-3.4.17-linux-x64").write_bytes(b"tampered")
    (tmp_path / "tailwindcss-3.4.17-linux-x64").chmod(0o755)
    calls: list[str] = []

    assert build_css.cached_binary("linux-x64", tmp_path) is None
    with pytest.raises(build_css.BinaryHashError):
        build_css.ensure_binary("linux-x64", tmp_path, fake_download(b"x", calls))

    assert calls == []
    assert no_subprocess == []


def test_main_refuses_a_bad_download_and_writes_nothing(
    tmp_path, monkeypatch, no_subprocess, caplog
):
    record = tmp_path / "inputs.sha256"
    monkeypatch.setattr(build_css, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(build_css, "HASH_RECORD", record)
    monkeypatch.setattr(build_css, "current_platform", lambda: ("Linux", "x86_64"))
    monkeypatch.setattr(build_css, "download_file", fake_download(b"evil", []))

    status = build_css.main([])

    assert status != 0
    assert "SHA-256" in caplog.text
    assert not record.exists()
    assert no_subprocess == []


def test_main_on_an_unsupported_platform_exits_with_a_message(
    monkeypatch, no_subprocess, caplog
):
    monkeypatch.setattr(build_css, "current_platform", lambda: ("Windows", "AMD64"))

    status = build_css.main([])

    assert status != 0
    assert "Windows" in caplog.text


# --- the build script: temp files, digest coverage, download cap ---------------------


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Point the script's outputs at a temporary tree with a stand-in binary."""
    static = tmp_path / "static"
    static.mkdir()
    sheet = static / "app.css"
    sheet.write_bytes(b"old css")
    record = tmp_path / "inputs.sha256"
    record.write_text("old digest\n")
    monkeypatch.setattr(build_css, "STYLESHEET", sheet)
    monkeypatch.setattr(build_css, "HASH_RECORD", record)
    monkeypatch.setattr(build_css, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(build_css, "current_platform", lambda: ("Linux", "x86_64"))
    monkeypatch.setattr(
        build_css, "ensure_binary", lambda key, cache, download: tmp_path / "tw"
    )
    return sheet, record


def files_under(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*") if p.is_file())


def test_failed_build_leaves_no_temp_file_and_touches_neither_output(
    fake_repo, monkeypatch, caplog
):
    sheet, record = fake_repo

    def failing_build(binary: Path, root: Path, output: Path) -> None:
        output.write_bytes(b"half written")
        raise subprocess.CalledProcessError(1, "tailwindcss", stderr=b"boom")

    monkeypatch.setattr(build_css, "build", failing_build)

    status = build_css.main([])

    assert status == 1
    assert "boom" in caplog.text
    assert files_under(sheet.parent.parent) == sorted([sheet, record])
    assert sheet.read_bytes() == b"old css"
    assert record.read_text() == "old digest\n"


def test_successful_build_replaces_the_stylesheet_and_records_the_digest(
    fake_repo, monkeypatch
):
    sheet, record = fake_repo
    monkeypatch.setattr(
        build_css, "build", lambda binary, root, output: output.write_bytes(b"new css")
    )

    status = build_css.main([])

    assert status == 0
    assert sheet.read_bytes() == b"new css"
    assert record.read_text().strip() == build_css.inputs_digest(build_css.ROOT)
    assert files_under(sheet.parent.parent) == sorted([sheet, record])
    assert list(sheet.parent.iterdir()) == [sheet]


def test_the_temp_file_is_never_created_in_the_served_directory(fake_repo, monkeypatch):
    sheet, _ = fake_repo
    seen: list[Path] = []

    def spying_build(binary: Path, root: Path, output: Path) -> None:
        seen.append(output)
        output.write_bytes(b"x")

    monkeypatch.setattr(build_css, "build", spying_build)

    build_css.main([])

    assert seen and seen[0].parent != sheet.parent


def test_download_hash_mismatch_says_the_download_may_be_bad_and_to_retry(tmp_path):
    with pytest.raises(build_css.BinaryHashError) as error:
        build_css.ensure_binary("linux-x64", tmp_path, fake_download(b"cut off", []))

    message = str(error.value)
    assert "incomplete" in message and "tampered" in message
    assert "again" in message and "retry" in message


def test_digest_changes_when_the_pinned_tailwind_version_changes(monkeypatch):
    before = build_css.inputs_digest(ROOT)

    monkeypatch.setattr(build_css, "TAILWIND_VERSION", "9.9.9")

    assert build_css.inputs_digest(ROOT) != before


def test_digest_changes_when_a_cli_argument_changes(monkeypatch):
    before = build_css.inputs_digest(ROOT)

    monkeypatch.setattr(
        build_css,
        "CLI_ARGUMENTS",
        [a for a in build_css.CLI_ARGUMENTS if a != "--minify"],
    )

    assert build_css.inputs_digest(ROOT) != before


def test_build_runs_the_cli_with_exactly_the_digested_arguments(tmp_path, monkeypatch):
    commands: list[list[str]] = []

    def record_run(command: list[str], **kwargs: object) -> None:
        commands.append(command)

    monkeypatch.setattr(subprocess, "run", record_run)

    build_css.build(Path("/bin/tw"), ROOT, tmp_path / "out.css")

    assert commands == [
        ["/bin/tw", *build_css.CLI_ARGUMENTS, "-o", str(tmp_path / "out.css")]
    ]


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._stream = io.BytesIO(payload)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self._stream.read(size)


def test_download_over_the_byte_cap_is_aborted(tmp_path, monkeypatch):
    monkeypatch.setattr(
        build_css.urllib.request,
        "urlopen",
        lambda url, timeout: _FakeResponse(b"x" * 5000),
    )
    destination = tmp_path / "file"

    with pytest.raises(build_css.DownloadTooLargeError, match="exceeded"):
        build_css.download_file("https://example.invalid/x", destination, max_bytes=100)

    assert destination.stat().st_size <= 100 + (1 << 20)


def test_oversized_download_leaves_nothing_in_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        build_css.urllib.request,
        "urlopen",
        lambda url, timeout: _FakeResponse(b"x" * 5000),
    )

    def capped(url: str, destination: Path) -> None:
        build_css.download_file(url, destination, max_bytes=100)

    with pytest.raises(build_css.DownloadTooLargeError):
        build_css.ensure_binary("linux-x64", tmp_path, capped)

    assert files_under(tmp_path) == []


def test_download_under_the_cap_is_written_whole(tmp_path, monkeypatch):
    monkeypatch.setattr(
        build_css.urllib.request,
        "urlopen",
        lambda url, timeout: _FakeResponse(b"y" * 50),
    )
    destination = tmp_path / "file"

    build_css.download_file("https://example.invalid/x", destination, max_bytes=100)

    assert destination.read_bytes() == b"y" * 50


# --- every class the page uses has a rule -------------------------------------------

TEMPLATES = ROOT / "src" / "templates"

# Literals inside Alpine class bindings that are compared values or ids, not classes.
NOT_CLASS_LITERALS = {
    "discovery": "tab name compared with activeTab",
    "pipeline": "tab name compared with activeTab",
    "settings": "tab name compared with activeTab",
    "{{ app.id }}": "Jinja package id used as an object key",
}
# Class tokens that legitimately have no CSS rule of their own. Keep each justified.
NO_RULE_TOKENS: dict[str, str] = {
    "group": (
        "Tailwind marker class; it exists only as the ancestor in "
        "`.group:hover .group-hover\\:...` rules, never as a rule of its own"
    ),
}

# Where the checker looks for classes: every `class`, `:class`, `x-bind:class` and
# `x-transition:*` class attribute (their value is a plain class list) of every `src/templates/*.html`, whatever the
# quote style or spacing around `=`; in the bound forms every quoted literal
# ('..', "..", `..`), which covers ternaries, objects, arrays and concatenation.
# A class counts as defined when the stylesheet, or the file's own <style> block,
# has a rule for it.
#
# NOT SUPPORTED, on purpose:
# - Strings in a <script> block (for example `cls: 'x'`) and JS-built class names
#   (`'bg-' + colour`). They are indistinguishable from ordinary words such as ids,
#   URLs and keys, so finding them needs a large hand-kept allow-list. Tailwind's
#   own scanner cannot see runtime-assembled names either. The one place the
#   page builds class names in script is the set of class-returning getters and
#   methods of index.html (scraperTextClass, scraperDotClass, secretBadgeClass,
#   secretMessageClass); they return whole literal strings, which Tailwind finds
#   in the raw file, and `test_every_class_the_script_can_return_has_a_rule`
#   runs them in node and checks each returned class. The always-on digest test
#   fails on any edit that is not rebuilt.
# - Classes added by Jinja, e.g. `class="a {{ x }}"`: raises, so it cannot pass
#   unnoticed.
CLASS_ATTRIBUTES = {
    "class",
    ":class",
    "x-bind:class",
    *(
        f"x-transition:{phase}"
        for phase in (
            "enter",
            "enter-start",
            "enter-end",
            "leave",
            "leave-start",
            "leave-end",
        )
    ),
}
STRING_LITERAL = re.compile(r"'([^']*)'|\"([^\"]*)\"|`([^`]*)`")
CSS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")

PLUGIN_CLASS = re.compile(
    r"(^|:)(prose|form-[a-z]+|aspect-[wh]-|@container|@[a-z0-9]+:|line-clamp-)"
)


@dataclass
class ClassUse:
    """Classes found in one template."""

    static: set[str] = field(default_factory=set)
    bound: set[str] = field(default_factory=set)
    style: str = ""


class _ClassCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.use = ClassUse()
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._in_style = tag == "style"
        for name, value in attrs:
            base = name.split(".")[0]
            if base not in CLASS_ATTRIBUTES or value is None:
                continue
            if base == "class" or base.startswith("x-transition:"):
                assert "{{" not in value and "{%" not in value, value
                self.use.static.update(value.split())
            else:
                self.use.bound.update(literal_words(value))

    def handle_endtag(self, tag: str) -> None:
        self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self.use.style += data


def literal_words(expression: str) -> set[str]:
    """Return the whitespace-separated words of every quoted literal."""
    words: set[str] = set()
    for match in STRING_LITERAL.finditer(expression):
        text = next(group for group in match.groups() if group is not None)
        if "{{" in text:
            words.add(text.strip())
            continue
        words.update(re.sub(r"\$\{[^}]*\}", " ", text).split())
    return words


def scan_template(html: str) -> ClassUse:
    """Collect the class tokens and `<style>` text of one template."""
    collector = _ClassCollector()
    collector.feed(html)
    collector.close()
    return collector.use


def escaped_class(token: str) -> str:
    """The class as it appears in a Tailwind selector."""
    escaped = re.sub(r"([^A-Za-z0-9_\-,])", r"\\\1", token)
    escaped = escaped.replace(",", "\\2c ")  # the one char Tailwind hex-escapes
    if token[0].isdigit():
        escaped = f"\\3{token[0]} {escaped[1:]}"
    return escaped


def has_own_rule(token: str, css: str) -> bool:
    """True if a rule with a declaration block targets the class itself.

    A selector that only names the class as an ancestor of another class
    (`.group:hover .group-hover\\:x`) does not count.
    """
    own = r"\." + re.escape(escaped_class(token)) + r"(?![\w\-\\])"
    ancestor = re.compile(own + r"(?::{1,2}[\w-]+(?:\([^)]*\))?)*(?:\s+|\s*[~+>]\s*)\.")
    for selectors, body in CSS_RULE.findall(css):
        if not body.strip():
            continue
        for selector in selectors.split(","):
            for hit in re.finditer(own, selector):
                if not ancestor.match(selector, hit.start()):
                    return True
    return False


def missing_classes(templates_dir: Path, css: str) -> list[str]:
    """Class tokens used by any template that have no rule and no allow-list entry."""
    missing: set[str] = set()
    for path in sorted(templates_dir.glob("*.html")):
        use = scan_template(path.read_text())
        bound = {t for t in use.bound if t not in NOT_CLASS_LITERALS}
        for token in use.static | bound:
            if token in NO_RULE_TOKENS or has_own_rule(token, css):
                continue
            if not has_own_rule(token, use.style):
                missing.add(token)
    return sorted(missing)


def used_tokens() -> tuple[set[str], set[str]]:
    """Return (static, bound) class tokens of the real templates."""
    static: set[str] = set()
    bound: set[str] = set()
    for path in sorted(TEMPLATES.glob("*.html")):
        use = scan_template(path.read_text())
        static |= use.static
        bound |= use.bound
    return static, bound


def test_every_class_in_every_template_has_a_rule_in_the_stylesheet():
    static, bound = used_tokens()

    assert len(static | bound) > 150
    assert missing_classes(TEMPLATES, STYLESHEET.read_text()) == []


def test_the_allow_lists_are_not_stale():
    css = STYLESHEET.read_text()
    static, bound = used_tokens()

    assert set(NOT_CLASS_LITERALS) <= bound
    assert set(NO_RULE_TOKENS) <= static | bound
    assert [t for t in NO_RULE_TOKENS if has_own_rule(t, css)] == []


@pytest.mark.parametrize(
    "token",
    [
        "z-[110]",
        "h-[550px]",
    ],
)
def test_the_arbitrary_value_classes_the_page_relies_on_are_in_the_stylesheet(token):
    static, bound = used_tokens()

    assert token in static | bound
    assert has_own_rule(token, STYLESHEET.read_text())


@pytest.mark.parametrize(
    ("token", "css", "expected"),
    [
        ("p-1", ".p-10{a:b}", False),
        ("p-1", ".p-1{a:b}", True),
        ("bg-x", ".bg-x{}", False),
        ("bg-x", '.a{content:".bg-x"}', False),
        ("hover:bg-white/[0.02]", r".hover\:bg-white\/\[0\.02\]:hover{a:b}", True),
        ("shadow-[a,b]", r".shadow-\[a\2c b\]{a:b}", True),
        ("group", ".group:hover .group-hover\\:x{a:b}", False),
        ("group", ".group:focus-within .group-hover\\:x{a:b}", False),
        ("peer", ".peer:checked~.peer-checked\\:x{a:b}", False),
        ("group-hover:x", ".group:hover .group-hover\\:x{a:b}", True),
        ("group", ".group{a:b}", True),
        ("selection:x", r".selection\:x ::selection{a:b}", True),
        ("container", "@media (min-width:640px){.container{max-width:640px}}", True),
        ("a", ".b,.a{x:y}", True),
    ],
)
def test_has_own_rule_requires_a_rule_with_a_body_that_targets_the_class(
    token, css, expected
):
    assert has_own_rule(token, css) is expected


def test_the_real_stylesheet_only_has_group_as_an_ancestor():
    css = STYLESHEET.read_text()

    assert not has_own_rule("group", css)
    assert re.search(r"\.group:hover \.group-hover", css)


INJECTIONS = {
    "static double quotes": '<div class="flex zz-missing"></div>',
    "static single quotes": "<div class='flex zz-missing'></div>",
    "spaces around equals": '<div class = "flex zz-missing"></div>',
    "ternary, second branch": "<div :class=\"a ? 'flex' : 'zz-missing'\"></div>",
    "ternary, first branch": "<div :class=\"a ? 'zz-missing' : 'flex'\"></div>",
    "object form": "<div :class=\"{ 'flex': a, 'zz-missing': b }\"></div>",
    "array form": "<div :class=\"['flex', a ? 'zz-missing' : '']\"></div>",
    "concatenation": "<div :class=\"'flex ' + 'zz-missing'\"></div>",
    "x-bind:class": "<div x-bind:class=\"'zz-missing'\"></div>",
    "single-quoted attribute": '<div :class=\'a ? "flex" : "zz-missing"\'></div>',
    "backtick template literal": '<div :class="`flex ${a} zz-missing`"></div>',
    "x-transition:enter": '<div x-transition:enter="zz-missing"></div>',
    "x-transition:enter-start": '<div x-transition:enter-start="zz-missing"></div>',
    "x-transition:enter-end": '<div x-transition:enter-end="zz-missing"></div>',
    "x-transition:leave": '<div x-transition:leave="zz-missing"></div>',
    "x-transition:leave-start": '<div x-transition:leave-start="zz-missing"></div>',
    "x-transition:leave-end": '<div x-transition:leave-end="zz-missing"></div>',
    "variant with arbitrary value": '<div class="hover:bg-zz/[0.5]"></div>',
}


@pytest.fixture
def template_copy(tmp_path):
    """A temporary copy of the templates; the real files are never touched."""
    copy_build_inputs(tmp_path)
    return tmp_path / "src" / "templates"


def inject(template: Path, snippet: str) -> None:
    html = template.read_text()
    assert "</body>" in html
    template.write_text(html.replace("</body>", f"{snippet}\n</body>", 1))


def test_an_unmodified_template_copy_reports_nothing(template_copy):
    assert missing_classes(template_copy, STYLESHEET.read_text()) == []


@pytest.mark.parametrize("position", sorted(INJECTIONS))
@pytest.mark.parametrize("template_name", ["index.html", "obtainium.html"])
def test_a_class_without_a_rule_is_reported_in_every_supported_position(
    template_copy, position, template_name
):
    inject(template_copy / template_name, INJECTIONS[position])

    missing = missing_classes(template_copy, STYLESHEET.read_text())

    assert len(missing) == 1
    assert missing[0].startswith(("zz-", "hover:bg-zz"))


def test_a_class_defined_in_the_templates_own_style_block_is_not_reported(
    template_copy,
):
    inject(template_copy / "obtainium.html", '<div class="card version"></div>')

    assert missing_classes(template_copy, STYLESHEET.read_text()) == []


def test_a_jinja_expression_inside_a_static_class_attribute_fails_loudly(
    template_copy,
):
    inject(template_copy / "index.html", '<div class="a {{ x }}"></div>')

    with pytest.raises(AssertionError):
        missing_classes(template_copy, STYLESHEET.read_text())


def test_fonts_are_vendored_served_and_licensed():
    stylesheet = STYLESHEET.read_text()
    urls = re.findall(r"url\(\"?(fonts/[^)\"]+)", stylesheet)

    assert len(urls) == 2
    for url in urls:
        assert (STATIC_DIR / url).is_file()
        assert "SIL OPEN FONT LICENSE" in (STATIC_DIR / "fonts" / "OFL.txt").read_text()
    assert "font-display:swap" in stylesheet
    assert "http" not in "".join(re.findall(r"@font-face\{[^}]*\}", stylesheet))


def test_page_uses_no_tailwind_plugin_classes_and_dark_follows_the_system():
    static, bound = used_tokens()

    plugin_hits = sorted(t for t in static | bound if PLUGIN_CLASS.search(t))
    dark_hits = sorted(t for t in static | bound if t.startswith("dark:"))
    css = STYLESHEET.read_text()

    assert plugin_hits == []
    assert dark_hits == []
    assert '"dark"' not in (ROOT / "tailwind" / "tailwind.config.js").read_text()
    assert "darkMode" in (ROOT / "tailwind" / "tailwind.config.js").read_text()
    assert "@media (prefers-color-scheme:dark){" in css
    assert ".dark " not in css


def test_every_class_the_script_can_return_has_a_rule(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    from tests.test_settings_ui import run_js

    page = main.templates.get_template("index.html").render(
        config={"apps": [], "sources": []}, available_apps=[], metadata={}, request=None
    )
    steps = """
      const out = [];
      for (const state of ['available', 'starting', 'stopping', 'unavailable', 'stopped']) {
        for (const scraper of [true, false]) {
          app.browser = {state}; app.diagnostics = {scraper};
          out.push(app.scraperTextClass, app.scraperDotClass);
        }
      }
      for (const source of ['environment', 'environment_invalid', 'file', null]) {
        app.secretsLoaded = true;
        app.secrets = {s: {set: source === 'file', source}};
        out.push(app.secretBadgeClass('s'));
      }
      app.secretsLoaded = false;
      out.push(app.secretBadgeClass('s'));
      for (const kind of ['ok', 'error', undefined]) {
        app.secretMessage = {s: kind ? {kind, text: 'x'} : null};
        out.push(app.secretMessageClass('s'));
      }
      return out;
    """
    returned = run_js(page, tmp_path, steps)

    tokens = {token for text in returned for token in text.split()}
    css = STYLESHEET.read_text()
    assert len(tokens) > 15
    assert sorted(t for t in tokens if not has_own_rule(t, css)) == []
