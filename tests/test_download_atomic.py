"""An interrupted release download never leaves a partial file at the final path."""

import pytest
import requests

from src import patcher


class FakeResponse:
    """A streamed response that optionally breaks after its chunks."""

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
