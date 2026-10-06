"""The suite-wide guard against real browsers and network connections."""

import socket

import pytest

from src import apkmirror_downloader


def test_real_browser_launch_is_blocked(no_real_browser_or_network):
    with pytest.raises(RuntimeError, match="real browser"):
        apkmirror_downloader.ApkMirror_Downloader()

    assert no_real_browser_or_network == ["browser launch"]
    no_real_browser_or_network.clear()


def test_network_connect_is_blocked(no_real_browser_or_network):
    with socket.socket() as sock, pytest.raises(OSError, match="network"):
        sock.connect(("127.0.0.1", 9))

    assert len(no_real_browser_or_network) == 1
    no_real_browser_or_network.clear()


def test_local_unix_sockets_still_work(tmp_path):
    path = str(tmp_path / "s.sock")
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(path)
        server.listen(1)
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(path)
