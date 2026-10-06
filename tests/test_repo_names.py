"""A repository name must be a real GitHub owner/name before it can build a URL."""

import pytest
import requests
from fastapi.testclient import TestClient

from src import config_store, main, patcher

ACCEPTED = [
    "owner/repo",
    "MorpheApp/morphe-patches",
    "a/b",
    "my-org/my.repo_name",
    "o1/.github",
    "x/a..b",
    "a" * 39 + "/" + "r" * 100,
]
REFUSED = [
    "./repo",
    "../repo",
    "owner/.",
    "owner/..",
    "owner/...",
    "./.",
    "../..",
    "owner/repo.git",
    "-owner/repo",
    "owner-/repo",
    "own_er/repo",
    "a" * 40 + "/repo",
    "owner/" + "r" * 101,
    "owner/re po",
    "owner/repo\n",
    "owner/re\x00po",
    "owner/repö",
    "öwner/repo",
    "ａ/repo",
    "owner/repo?x=1",
    "owner/repo#x",
    "owner/re%2Fpo",
    "owner",
    "owner/repo/extra",
    "/repo",
    "owner/",
    "",
]


@pytest.mark.parametrize("repo", ACCEPTED)
def test_real_names_are_valid(repo):
    assert patcher.is_valid_repo(repo) is True


@pytest.mark.parametrize("repo", REFUSED)
def test_names_that_steer_a_url_are_invalid(repo):
    assert patcher.is_valid_repo(repo) is False


@pytest.mark.parametrize("repo", [None, 5, ["a", "b"], {"a": "b"}])
def test_a_non_string_is_invalid(repo):
    assert patcher.is_valid_repo(repo) is False


@pytest.mark.parametrize(
    "pasted",
    ["https://github.com/../..", "github.com/./repo", "../..", "owner/..", "a/b\n/c"],
)
def test_normalize_refuses_dot_segments(pasted):
    assert patcher.normalize_repo(pasted) is None


def test_normalize_still_accepts_urls_and_git_suffix():
    assert patcher.normalize_repo("https://github.com/own/repo.git") == "own/repo"
    assert patcher.normalize_repo("www.github.com/own/repo/tree/main") == "own/repo"


@pytest.fixture
def requests_made(monkeypatch):
    """Record every HTTP call; none is answered."""
    calls: list[str] = []

    def record(url, *args, **kwargs):
        calls.append(url)
        raise requests.exceptions.ConnectionError("blocked")

    monkeypatch.setattr(patcher.requests, "get", record)
    return calls


@pytest.mark.parametrize("repo", ["..", "../..", "a/..", "./b", "a/b/c", ""])
def test_sync_of_an_invalid_repo_makes_no_request(
    data_dir, requests_made, repo, caplog
):
    paths = {".jar": data_dir / "bin" / "x.jar"}

    result = patcher.check_and_download_release(repo, paths, force=True)

    assert result is False
    assert requests_made == []
    assert "invalid repository" in caplog.text.lower()


def test_update_tools_skips_a_hand_edited_source_and_syncs_the_rest(
    data_dir, requests_made
):
    config = {
        "repositories": {"cli": "x/cli", "apkeditor": "x/edit"},
        "tools": {"cli_jar": "bin/cli.jar", "apkeditor_jar": "bin/edit.jar"},
        "sources": [
            {"name": "bad", "repo": "../..", "active": True},
            {"name": "good", "repo": "own/src", "active": True},
        ],
    }

    patcher.update_tools(config, quiet=True, force=True)

    assert "own/src" in "".join(requests_made)
    assert not any(".." in url for url in requests_made)
    assert all(url.startswith("https://api.github.com/repos/") for url in requests_made)
    assert not any("/repos/../" in url or "/repos/./" in url for url in requests_made)


def test_update_tools_refuses_an_invalid_tool_repo(data_dir, requests_made):
    config = {
        "repositories": {"cli": "../..", "apkeditor": "x/edit"},
        "tools": {"cli_jar": "bin/cli.jar", "apkeditor_jar": "bin/edit.jar"},
        "sources": [],
    }

    patcher.update_tools(config, quiet=True, force=True)

    assert all("/repos/x/edit/" in url for url in requests_made)


def test_add_source_refuses_dot_segments_without_a_request(data_dir, requests_made):
    client = TestClient(main.app)

    for repo in ("../..", "owner/..", "./x", "https://github.com/./.."):
        response = client.post("/api/ui/add_source", json={"repo": repo})
        assert response.status_code == 400

    assert requests_made == []
    saved = config_store.load_config()
    assert [s["repo"] for s in saved["sources"]] == ["own/src"]


def test_an_invalid_source_can_still_be_removed(data_dir):
    def add_bad(config: dict) -> None:
        config["sources"].append({"name": "bad", "repo": "../..", "active": True})

    main.update_config(add_bad)
    client = TestClient(main.app)

    response = client.post("/api/ui/delete_source", json={"repo": "../.."})

    assert response.status_code == 200
    saved = config_store.load_config()
    assert [s["repo"] for s in saved["sources"]] == ["own/src"]
