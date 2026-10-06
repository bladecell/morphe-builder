"""Release sync review fixes: tag change atomicity, list shape, order, size, tags."""

import json
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import pytest
import requests

from src import patcher

REPO = "own/src"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RAW_ROOT = f"https://raw.githubusercontent.com/{REPO}/"
CHUNK = 64 * 1024

GOOD_LIST = json.dumps({"patches": [{"name": "Good", "compatiblePackages": {}}]})
OLD_LIST = json.dumps({"patches": [{"name": "Old", "compatiblePackages": {}}]})
TOOLING = json.dumps([{"name": "tooling-shaped", "compatiblePackages": {}}])


class FakeResponse:
    """Streaming response: bytes are served lazily so over-reads are visible."""
    is_redirect = False

    def __init__(self, net: "Net", url: str, body: bytes | int | dict | None) -> None:
        self.net = net
        self.url = url
        self.body = body
        self.status_code = 404 if body is None else 200
        self.headers: dict[str, str] = {}
        if isinstance(body, bytes):
            self.headers["content-length"] = str(len(body))

    def close(self) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} for {self.url}")

    def json(self) -> dict:
        assert isinstance(self.body, dict)
        return self.body

    def iter_content(self, size: int) -> Iterator[bytes]:
        if isinstance(self.body, int):
            remaining = self.body
            while remaining > 0:
                piece = min(CHUNK, remaining)
                remaining -= piece
                self.net.served += piece
                yield b"x" * piece
        elif isinstance(self.body, bytes):
            self.net.served += len(self.body)
            yield self.body


class Net:
    """URL -> body. An int body is a stream of that many bytes."""

    def __init__(self, release: dict, files: dict[str, bytes | int]) -> None:
        self.release = release
        self.files = files
        self.urls: list[str] = []
        self.served = 0

    def get(self, url: str, *args: object, **kwargs: object) -> FakeResponse:
        self.urls.append(url)
        if url == API_URL:
            return FakeResponse(self, url, self.release)
        return FakeResponse(self, url, self.files.get(url))


def build_release(
    tag: str,
    assets: list[tuple[str, int | None]],
    slash_form: str = "literal",
) -> dict:
    """Build a release object; `slash_form` is `literal` or `encoded`."""
    tag_path = quote(tag, safe="/+" if slash_form == "literal" else "+")
    base = f"https://github.com/{REPO}/releases/download/{tag_path}/"
    entries: list[dict] = []
    for name, size in assets:
        entry: dict = {"name": name, "browser_download_url": base + name}
        if size is not None:
            entry["size"] = size
        entries.append(entry)
    return {"tag_name": tag, "assets": entries}


def asset_url(release: dict, name: str) -> str:
    return next(
        a["browser_download_url"] for a in release["assets"] if a["name"] == name
    )


@pytest.fixture
def paths(data_dir, monkeypatch) -> dict[str, Path]:
    monkeypatch.setattr(patcher, "VERSIONS_FILE", data_dir / "bin" / "versions.json")
    source_paths = patcher.get_source_paths(REPO)
    source_paths[".mpp"].parent.mkdir(parents=True, exist_ok=True)
    return source_paths


def install(monkeypatch, net: Net) -> Net:
    monkeypatch.setattr(patcher.requests, "get", net.get)
    return net


def synced_old(paths: dict[str, Path], tag: str = "v1") -> None:
    paths[".mpp"].write_text("old mpp")
    paths[".json"].write_text(OLD_LIST)
    patcher.save_local_versions({REPO: tag})


def stored_names() -> list[object]:
    return [p["name"] for p in patcher.load_patch_list(REPO)]


# --- item 1: bundle and list always belong to the same tag -------------------


def test_new_release_without_a_list_stays_unsynced(paths, monkeypatch, capsys):
    synced_old(paths)
    release = build_release("v2", [("patches-2.mpp", 1000)])
    install(monkeypatch, Net(release, {asset_url(release, "patches-2.mpp"): b"new"}))

    changed = patcher.check_and_download_release(REPO, paths, force=True)

    assert changed is False
    assert patcher.load_local_versions()[REPO] == "v1"
    assert paths[".mpp"].read_text() == "old mpp"
    assert paths[".json"].read_text() == OLD_LIST
    assert "Not marking as synced" in capsys.readouterr().out


def test_failed_bundle_download_does_not_replace_the_old_list(paths, monkeypatch):
    synced_old(paths)
    release = build_release("v2", [("patches-2.mpp", 1000), ("patches-list.json", 90)])
    files = {asset_url(release, "patches-list.json"): GOOD_LIST.encode()}
    install(monkeypatch, Net(release, files))

    changed = patcher.check_and_download_release(REPO, paths, force=True)

    assert changed is False
    assert paths[".json"].read_text() == OLD_LIST
    assert paths[".mpp"].read_text() == "old mpp"
    assert patcher.load_local_versions()[REPO] == "v1"


def test_successful_new_release_replaces_both_and_leaves_no_staging(paths, monkeypatch):
    synced_old(paths)
    release = build_release("v2", [("patches-2.mpp", 1000), ("patches-list.json", 90)])
    files = {
        asset_url(release, "patches-2.mpp"): b"new mpp",
        asset_url(release, "patches-list.json"): GOOD_LIST.encode(),
    }
    install(monkeypatch, Net(release, files))

    changed = patcher.check_and_download_release(REPO, paths, force=True)

    assert changed is True
    assert paths[".mpp"].read_text() == "new mpp"
    assert stored_names() == ["Good"]
    assert patcher.load_local_versions()[REPO] == "v2"
    assert sorted(p.name for p in paths[".mpp"].parent.iterdir() if p.is_file()) == (
        sorted([paths[".mpp"].name, paths[".json"].name, "versions.json"])
    )


# --- item 2: what counts as a patch list --------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        '{"patches": []}',
        '[{"file": "x"}]',
        '{"patches": [{"name": 5}, {"name": null}]}',
        '{"patches": [{"compatiblePackages": {}}]}',
    ],
)
def test_file_without_a_named_patch_is_not_a_patch_list(tmp_path, content):
    path = tmp_path / "list.json"
    path.write_text(content)

    with pytest.raises(patcher._PatchListFormatError):
        patcher._read_patch_list(path)


@pytest.mark.parametrize(
    "content",
    [
        '[{"name": "A"}]',
        '{"patches": [{"name": "A"}]}',
        '{"patches": [{"name": "A"}, {"name": null}, {"x": 1}]}',
    ],
)
def test_both_real_shapes_are_still_patch_lists(tmp_path, content):
    path = tmp_path / "list.json"
    path.write_text(content)

    assert patcher._read_patch_list(path)[0]["name"] == "A"


def test_empty_patches_json_never_replaces_a_readable_list(paths, monkeypatch):
    synced_old(paths)
    release = build_release("v1", [("patches.json", 20), ("patches-1.mpp", 10)])
    files = {
        asset_url(release, "patches.json"): b'{"patches": []}',
        asset_url(release, "patches-1.mpp"): b"new mpp",
    }
    net = install(monkeypatch, Net(release, files))
    paths[".mpp"].unlink()

    changed = patcher.check_and_download_release(REPO, paths, force=True)

    # The bundle was fetched and the list was considered: the empty one lost.
    assert asset_url(release, "patches.json") in net.urls
    assert changed is True
    assert paths[".mpp"].read_text() == "new mpp"
    assert paths[".json"].read_text() == OLD_LIST


# --- item 3: candidate order ---------------------------------------------------


def test_repo_list_at_the_tag_beats_an_unnamed_asset(paths, monkeypatch):
    release = build_release(
        "v2", [("patches-2.mpp", 10), ("zz-tooling.json", 50)], "literal"
    )
    files = {
        asset_url(release, "patches-2.mpp"): b"mpp",
        asset_url(release, "zz-tooling.json"): TOOLING.encode(),
        RAW_ROOT + "v2/patches-list.json": GOOD_LIST.encode(),
    }
    net = install(monkeypatch, Net(release, files))

    assert patcher.check_and_download_release(REPO, paths, force=True) is True

    assert stored_names() == ["Good"]
    assert asset_url(release, "zz-tooling.json") not in net.urls


def test_unnamed_asset_is_used_when_nothing_better_exists(paths, monkeypatch):
    release = build_release("v2", [("patches-2.mpp", 10), ("morphe-list.json", 50)])
    files = {
        asset_url(release, "patches-2.mpp"): b"mpp",
        asset_url(release, "morphe-list.json"): GOOD_LIST.encode(),
    }
    install(monkeypatch, Net(release, files))

    assert patcher.check_and_download_release(REPO, paths, force=True) is True
    assert stored_names() == ["Good"]


# --- item 4: unknown size, suffix case ----------------------------------------


def test_asset_without_a_size_is_not_downloaded_unbounded(paths, monkeypatch):
    release = build_release("v2", [("patches-2.mpp", 10), ("mystery.json", None)])
    files = {
        asset_url(release, "patches-2.mpp"): b"mpp",
        asset_url(release, "mystery.json"): 50 * 1024 * 1024,
    }
    net = install(monkeypatch, Net(release, files))

    changed = patcher.check_and_download_release(REPO, paths, force=True)

    assert changed is False
    assert net.served < 3 * 1024 * 1024


def test_json_suffix_check_ignores_case(paths, monkeypatch):
    release = build_release("v2", [("patches-2.mpp", 10), ("Patches-List.JSON", 50)])
    files = {
        asset_url(release, "patches-2.mpp"): b"mpp",
        asset_url(release, "Patches-List.JSON"): GOOD_LIST.encode(),
    }
    install(monkeypatch, Net(release, files))

    assert patcher.check_and_download_release(REPO, paths, force=True) is True
    assert stored_names() == ["Good"]


# --- item 5: tags with a slash --------------------------------------------------


@pytest.mark.parametrize("form", ["literal", "encoded"])
def test_slash_tag_syncs_with_either_url_form(paths, monkeypatch, form):
    release = build_release(
        "release/1.0", [("patches-1.mpp", 10), ("patches-list.json", 50)], form
    )
    files = {
        asset_url(release, "patches-1.mpp"): b"mpp",
        asset_url(release, "patches-list.json"): GOOD_LIST.encode(),
    }
    install(monkeypatch, Net(release, files))

    assert patcher.check_and_download_release(REPO, paths, force=True) is True
    assert patcher.load_local_versions()[REPO] == "release/1.0"
    assert stored_names() == ["Good"]


def test_slash_tag_raw_fallback_keeps_the_slash(paths, monkeypatch):
    release = build_release("release/1.0", [("patches-1.mpp", 10)])
    files = {
        asset_url(release, "patches-1.mpp"): b"mpp",
        RAW_ROOT + "release/1.0/patches-list.json": GOOD_LIST.encode(),
    }
    install(monkeypatch, Net(release, files))

    assert patcher.check_and_download_release(REPO, paths, force=True) is True
    assert stored_names() == ["Good"]


@pytest.mark.parametrize(
    "tag",
    ["release/1.0", "a/b/c", "v1", "1.0+build", "feature/x_y-z"],
)
def test_valid_refs(tag):
    assert patcher._is_valid_ref(tag) is True


@pytest.mark.parametrize(
    "tag",
    ["a/../b", "a//b", "/a", "a/", "a/./b", "..", ".", "a%2Fb", "a?b", "a#b",
     "a\\b", "a/%2e%2e/b", "-a", "a/-b", "a b", "x" * 201, "", None, 5],
)  # fmt: skip
def test_invalid_refs(tag):
    assert patcher._is_valid_ref(tag) is False


def asset_ok(url: str, tag: str = "release/1.0", name: str = "p.mpp") -> bool:
    return patcher._is_release_asset_url(url, tag, name)


@pytest.mark.parametrize(
    "path",
    [
        "release/1.0/p.mpp",
        "release%2F1.0/p.mpp",
    ],
)
def test_asset_url_accepts_the_literal_and_the_encoded_slash(path):
    assert asset_ok(f"https://github.com/{REPO}/releases/download/{path}") is True


@pytest.mark.parametrize(
    "path",
    [
        "release/1.0/../p.mpp",
        "release//1.0/p.mpp",
        "release/%2e%2e/1.0/p.mpp",
        "release%252F1.0/p.mpp",
        "release/1.0/p.mpp?x=1",
        "release/1.0/p.mpp#f",
        "release/1.0/other.mpp",
        "other/1.0/p.mpp",
        "release\\1.0/p.mpp",
        "release/1.0%2Fp.mpp",
    ],
)
def test_asset_url_refuses_other_shapes(path):
    assert asset_ok(f"https://github.com/{REPO}/releases/download/{path}") is False


def test_asset_url_refuses_a_dot_dot_name():
    url = f"https://github.com/{REPO}/releases/download/v1/.."

    assert asset_ok(url, tag="v1", name="..") is False


def test_slash_tag_cannot_leave_the_patched_directory():
    version = patcher.build_combined_version(
        "1.0", {REPO: "release/1.0"}, {"pkg": ["Patch"]}
    )

    assert "/" not in version
    assert version.startswith("1.0-release-1.0-")


# --- item 6: last-resort oversized lists ----------------------------------------


def big_list(size: int) -> bytes:
    entry = json.dumps({"name": "Big", "compatiblePackages": {}})
    padding = " " * max(0, size - len(entry) - 2)
    return ("[" + entry + padding + "]").encode()


def test_oversized_unnamed_list_is_used_as_a_last_resort(paths, monkeypatch, capsys):
    size = 300 * 1024
    release = build_release("v2", [("patches-2.mpp", 10), ("weird-name.json", size)])
    files = {
        asset_url(release, "patches-2.mpp"): b"mpp",
        asset_url(release, "weird-name.json"): big_list(size),
    }
    install(monkeypatch, Net(release, files))

    assert patcher.check_and_download_release(REPO, paths, force=True) is True
    assert stored_names() == ["Big"]
    assert "weird-name.json" in capsys.readouterr().out


def test_oversized_tooling_is_never_downloaded_when_a_repo_list_resolved(
    paths, monkeypatch
):
    release = build_release("v2", [("patches-2.mpp", 10), ("big.json", 500 * 1024)])
    files = {
        asset_url(release, "patches-2.mpp"): b"mpp",
        asset_url(release, "big.json"): big_list(500 * 1024),
        RAW_ROOT + "v2/patches-list.json": GOOD_LIST.encode(),
    }
    net = install(monkeypatch, Net(release, files))

    assert patcher.check_and_download_release(REPO, paths, force=True) is True
    assert stored_names() == ["Good"]
    assert asset_url(release, "big.json") not in net.urls


def test_oversized_pass_goes_smallest_first_and_skips_above_the_cap(
    paths, monkeypatch, capsys
):
    release = build_release(
        "v2",
        [
            ("patches-2.mpp", 10),
            ("huge.json", 3 * 1024 * 1024),
            ("b.json", 400 * 1024),
            ("a.json", 300 * 1024),
        ],
    )
    files = {
        asset_url(release, "patches-2.mpp"): b"mpp",
        asset_url(release, "a.json"): b'{"patches": []}'.ljust(300 * 1024, b" "),
        asset_url(release, "b.json"): big_list(400 * 1024),
    }
    net = install(monkeypatch, Net(release, files))

    assert patcher.check_and_download_release(REPO, paths, force=True) is True

    tried = [u for u in net.urls if u.endswith(".json") and "github.com" in u]
    assert [u.rsplit("/", 1)[-1] for u in tried] == ["a.json", "b.json"]
    assert "huge.json" in capsys.readouterr().out


def test_declared_size_is_not_trusted_for_the_cap(paths, monkeypatch):
    release = build_release("v2", [("patches-2.mpp", 10), ("liar.json", 400 * 1024)])
    files = {
        asset_url(release, "patches-2.mpp"): b"mpp",
        asset_url(release, "liar.json"): 40 * 1024 * 1024,
    }
    net = install(monkeypatch, Net(release, files))

    changed = patcher.check_and_download_release(REPO, paths, force=True)

    assert changed is False
    assert net.served < 3 * 1024 * 1024
    assert not list(paths[".json"].parent.glob("*.candidate"))
    assert not list(paths[".json"].parent.glob("*.staged"))
