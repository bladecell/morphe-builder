"""An interrupted release download never leaves a partial file at the final path."""

import pytest
import requests

from src import patcher


class FakeResponse:
    """A streamed response that optionally breaks after its chunks."""
    is_redirect = False

    def __init__(self, chunks: list[bytes], fail_after: bool = False) -> None:
        self.chunks = chunks
        self.fail_after = fail_after
        self.headers = {"content-length": str(sum(len(c) for c in chunks))}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, block_size: int):
        yield from self.chunks
        if self.fail_after:
            raise requests.exceptions.ChunkedEncodingError("connection lost")


def serve(monkeypatch, response: FakeResponse) -> None:
    monkeypatch.setattr(patcher.requests, "get", lambda *a, **k: response)


def test_interrupted_download_leaves_no_file(tmp_path, monkeypatch):
    target = tmp_path / "patches.json"
    serve(monkeypatch, FakeResponse([b'{"patches": [{"na'], fail_after=True))

    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        patcher.download_with_progress("https://example.invalid/p.json", target)

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_interrupted_download_keeps_the_previous_file(tmp_path, monkeypatch):
    target = tmp_path / "patches.json"
    target.write_text('{"patches": []}')
    serve(monkeypatch, FakeResponse([b'{"patches": [{"na'], fail_after=True))

    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        patcher.download_with_progress("https://example.invalid/p.json", target)

    assert target.read_text() == '{"patches": []}'
    assert [p.name for p in tmp_path.iterdir()] == ["patches.json"]


def test_complete_download_replaces_the_file(tmp_path, monkeypatch):
    target = tmp_path / "patches.json"
    target.write_text("old")
    serve(monkeypatch, FakeResponse([b'{"patches"', b": []}"]))

    patcher.download_with_progress("https://example.invalid/p.json", target)

    assert target.read_text() == '{"patches": []}'
    assert [p.name for p in tmp_path.iterdir()] == ["patches.json"]


def test_two_downloads_of_one_target_never_share_a_temp_file(tmp_path, monkeypatch):
    target = tmp_path / "patches.json"
    url = "https://example.invalid/p.json"
    calls: list[int] = []
    target_while_outer_runs: list[str] = []

    def fake_stream(_url: str, temp_path) -> None:
        calls.append(len(calls))
        if calls == [0]:
            temp_path.write_bytes(b"outer-1")
            patcher.download_with_progress(url, target)  # a second, complete download
            target_while_outer_runs.append(target.read_text())
            with open(temp_path, "ab") as handle:
                handle.write(b"+outer-2")
        else:
            temp_path.write_bytes(b"inner")

    monkeypatch.setattr(patcher, "_stream_to_file", fake_stream)

    patcher.download_with_progress(url, target)

    assert target_while_outer_runs == ["inner"]
    assert target.read_text() == "outer-1+outer-2"
    assert [p.name for p in tmp_path.iterdir()] == ["patches.json"]


def test_temp_file_sits_next_to_the_target_and_is_unique(tmp_path, monkeypatch):
    target = tmp_path / "patches.json"
    temp_paths = []

    def fake_stream(_url: str, temp_path) -> None:
        temp_paths.append(temp_path)
        temp_path.write_bytes(b"x")

    monkeypatch.setattr(patcher, "_stream_to_file", fake_stream)

    patcher.download_with_progress("https://example.invalid/a", target)
    patcher.download_with_progress("https://example.invalid/b", target)

    assert temp_paths[0] != temp_paths[1]
    assert {p.parent for p in temp_paths} == {tmp_path}
    assert all(p.name.startswith("patches.json.") for p in temp_paths)
