"""Refuse cross-site and DNS-rebinding requests to the unauthenticated app.

Two rules, applied to every HTTP request by `RequestGuardMiddleware`:

1. Host allow-list (against DNS rebinding). The Host header, port ignored, must be
   an IP literal (IPv4, or IPv6 in brackets), `localhost`, the host of
   `settings.server_url`, or a name in the comma-separated `ALLOWED_HOSTS`
   environment variable (a bare name, or the host of a full-origin entry).
   Otherwise the answer is 400. `X-Forwarded-Host` is never consulted: any client
   can send it, so trusting it would defeat the rule.
2. Origin check (against cross-site requests from a browser). A request whose
   method is not GET, HEAD or OPTIONS and that carries an Origin header must have
   an Origin whose host and port equal the Host header, or the `server_url` host
   and port, or exactly match (scheme, host, port) an `ALLOWED_HOSTS` entry written
   as a full origin such as `https://morphe.example.com`. A bare name in
   `ALLOWED_HOSTS` allows that Host and nothing more. Otherwise the answer is 403.
   A missing Origin passes (Obtainium, curl); `Origin: null` (sandboxed frames,
   file pages, some redirects) is refused.

Loopback (localhost, 127.0.0.0/8, ::1) is only trusted as an Origin through the
server URL or `ALLOWED_HOSTS` when the request's Host is loopback too.

REVERSE PROXIES: the rules read the Host header the app receives. A proxy must pass
the client's original Host through (nginx `proxy_set_header Host $host`, Caddy and
Traefik do by default). A proxy that rewrites Host to a loopback address (for
example a same-machine proxy forwarding to `127.0.0.1`) makes every request look
local, so the loopback rule and the rebinding protection do not hold; a proxy that
rewrites Host to another address needs the public origin in `ALLOWED_HOSTS` or a
non-loopback server URL.

The decision logic is in pure functions; only `read_server_url` touches the disk.
"""

import ipaddress
import logging
import os
import re
import traceback
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import yaml  # type: ignore[import-untyped]
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src import config_store, secrets_store

logger = logging.getLogger(__name__)

HOST_REFUSED = (
    "Host not allowed. Add this name to the ALLOWED_HOSTS environment variable "
    "or set the server URL in settings."
)
ORIGIN_REFUSED = (
    "Cross-site request refused. If the app is behind a reverse proxy or served "
    "under another name, add its full origin (for example "
    "https://morphe.example.com) to the ALLOWED_HOSTS environment variable or set "
    "the server URL in settings."
)
INTERNAL_ERROR = "Internal Server Error"
FRAME_HEADERS = (
    (b"content-security-policy", b"frame-ancestors 'none'"),
    (b"x-frame-options", b"DENY"),
)
# Every CSP header is enforced, so an app policy is kept and ours is added; a
# second X-Frame-Options with another value is ignored by browsers, so it is
# replaced.
_REPLACED_HEADERS = frozenset({b"x-frame-options"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
DEFAULT_PORTS = {"http": 80, "https": 443}

_HOST_PATTERN = re.compile(
    r"(?:\[(?P<v6>[0-9a-f:.]{2,45})\]|(?P<name>[a-z0-9][a-z0-9._-]{0,252}))"
    r"(?::(?P<port>[0-9]{1,5}))?"
)
_ORIGIN_PATTERN = re.compile(r"(?P<scheme>https?)://(?P<authority>[^/?#\s]+)")


class HostParts(NamedTuple):
    """A parsed Host header: lowercase name without trailing dot, port, IP flag."""

    name: str
    port: int | None
    is_ip: bool


def parse_host(header: str | None) -> HostParts | None:
    """Parse a Host header or URL authority strictly.

    Args:
        header: Text such as `Example.com:8000`, `[::1]` or `127.0.0.1`.

    Returns:
        The parts, or None when anything unusual is present (userinfo, path,
        whitespace, control or non-ASCII characters, bad port, unbracketed IPv6).
    """
    if not isinstance(header, str) or not header.isascii():
        return None
    match = _HOST_PATTERN.fullmatch(header.lower())
    if match is None:
        return None
    port = int(match["port"]) if match["port"] is not None else None
    if port is not None and port > 65535:
        return None
    if match["v6"] is not None:
        try:
            return HostParts(str(ipaddress.IPv6Address(match["v6"])), port, True)
        except ValueError:
            return None
    name = match["name"].removesuffix(".")
    if not name:
        return None
    try:
        ipaddress.IPv4Address(name)
    except ValueError:
        return HostParts(name, port, False)
    return HostParts(name, port, True)


def _parse_origin_entry(entry: str) -> tuple[str, str, int] | None:
    """Parse a full-origin `ALLOWED_HOSTS` entry into (scheme, name, port).

    One trailing slash is tolerated. Bare names and anything with a path,
    userinfo or a bad port give None.
    """
    match = _ORIGIN_PATTERN.fullmatch(entry.strip().lower().removesuffix("/"))
    if match is None:
        return None
    parts = parse_host(match["authority"])
    if parts is None:
        return None
    return match["scheme"], parts.name, parts.port or DEFAULT_PORTS[match["scheme"]]


def _entry_authority(entry: str) -> str:
    """Return the host part of an `ALLOWED_HOSTS` entry, bare or full origin."""
    text = entry.strip()
    origin = _ORIGIN_PATTERN.fullmatch(text.lower().removesuffix("/"))
    return origin["authority"] if origin else text


def allowed_host_names(server_url: object, allowed_hosts: str | None) -> frozenset[str]:
    """Collect the configured host names beyond IP literals and localhost.

    Args:
        server_url: `settings.server_url` from the config; anything unusable is
            ignored.
        allowed_hosts: Raw `ALLOWED_HOSTS` value, comma-separated. An entry is a
            bare name (port ignored) or a full origin (its host counts); junk
            entries are skipped.

    Returns:
        Lowercase names without trailing dots.
    """
    entries = [_entry_authority(e) for e in (allowed_hosts or "").split(",")]
    if isinstance(server_url, str):
        try:
            entries.append(urlsplit(server_url).netloc)
        except ValueError:
            logger.warning("Ignoring an unparsable server URL in the config")
    parts = (parse_host(entry.strip()) for entry in entries)
    return frozenset(p.name for p in parts if p is not None)


def is_host_allowed(header: str | None, extra_names: frozenset[str]) -> bool:
    """Tell whether a Host header passes the allow-list.

    Args:
        header: Raw Host header, or None when it is missing.
        extra_names: Result of `allowed_host_names`.

    Returns:
        True for IP literals, `localhost` and the extra names.
    """
    parts = parse_host(header)
    if parts is None:
        return False
    return parts.is_ip or parts.name == "localhost" or parts.name in extra_names


def _origin_address(origin: str) -> tuple[str, int, int, str] | None:
    """Return (name, port, scheme default port, scheme) of an Origin value."""
    match = _ORIGIN_PATTERN.fullmatch(origin.lower())
    if match is None:
        return None
    parts = parse_host(match["authority"])
    if parts is None:
        return None
    default_port = DEFAULT_PORTS[match["scheme"]]
    return parts.name, parts.port or default_port, default_port, match["scheme"]


def _url_address(url: object) -> tuple[str, int] | None:
    """Return (name, effective port) of the server URL, or None if unusable."""
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url.strip().lower())
    except ValueError:
        return None
    if parsed.scheme not in DEFAULT_PORTS:
        return None
    parts = parse_host(parsed.netloc)
    if parts is None:
        return None
    return parts.name, parts.port or DEFAULT_PORTS[parsed.scheme]


def is_loopback(name: str) -> bool:
    """Tell whether a parsed host name (see `parse_host`) means this machine.

    Args:
        name: Lowercase name or IP text without brackets or port.

    Returns:
        True for `localhost`, `*.localhost`, 127.0.0.0/8, ::1 (also IPv4-mapped)
        and the unspecified addresses 0.0.0.0 and ::.
    """
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


def _allowed_hosts_match(
    scheme: str, name: str, port: int, allowed_hosts: str | None
) -> bool:
    """Tell whether an Origin equals a full-origin `ALLOWED_HOSTS` entry.

    Scheme, host and port must all be equal; a missing port in the entry means the
    default port of its scheme. Bare names never match.
    """
    return any(
        _parse_origin_entry(entry) == (scheme, name, port)
        for entry in (allowed_hosts or "").split(",")
    )


def is_origin_allowed(
    origin: str,
    host_header: str | None,
    server_url: object,
    allowed_hosts: str | None = None,
) -> bool:
    """Tell whether an Origin header names this app's own address.

    Args:
        origin: Raw Origin header. `null` and anything that is not
            `http(s)://host[:port]` is refused.
        host_header: Raw Host header of the same request.
        server_url: `settings.server_url`, accepted as another own address.
        allowed_hosts: Raw `ALLOWED_HOSTS` value; only entries written as a full
            origin (`https://morphe.example.com[:port]`) are accepted as Origins.

    Returns:
        True when the Origin host and port equal the Host header's (a Host
        without a port takes the default port of the Origin scheme), or the
        server URL's, or an `ALLOWED_HOSTS` full-origin entry's (scheme included).
        A server URL or entry that
        is loopback only counts when the request Host is loopback too, so a
        loopback default cannot vouch for a page served from another machine.
        The scheme itself is not compared: behind a TLS proxy it legitimately
        differs.
    """
    address = _origin_address(origin)
    if address is None:
        return False
    name, port, default_port, scheme = address
    host = parse_host(host_header)
    if host is not None and (name, port) == (host.name, host.port or default_port):
        return True
    if host is None or (is_loopback(name) and not is_loopback(host.name)):
        return False
    return (name, port) == _url_address(server_url) or _allowed_hosts_match(
        scheme, name, port, allowed_hosts
    )


def is_state_changing(method: str) -> bool:
    """Tell whether an HTTP method may change state (anything but GET/HEAD/OPTIONS)."""
    return method.upper() not in SAFE_METHODS


_server_url_cache: dict[str, object] = {"key": None, "value": None}


def read_server_url() -> str | None:
    """Return `settings.server_url` from config.yaml without side effects.

    Unlike `config_store.load_config` this never seeds a missing file and takes no
    lock. The result, including a failure, is cached per file version (inode,
    mtime, size) so a broken file warns once, not on every request. Any problem
    returns None, so the allow-list falls back to IP literals, localhost and
    `ALLOWED_HOSTS` (fail closed).
    """
    path = config_store.DATA_DIR / "config.yaml"
    try:
        info = path.stat()
        key: tuple[object, ...] = (
            str(path),
            info.st_ino,
            info.st_mtime_ns,
            info.st_size,
        )
    except OSError:
        key = (str(path), "unreadable")
    if _server_url_cache["key"] == key:
        value = _server_url_cache["value"]
        return value if isinstance(value, str) else None
    value = _load_server_url(path)
    _server_url_cache.update(key=key, value=value)
    return value


def _load_server_url(path: Path) -> str | None:
    """Parse `settings.server_url` from `path`; None (and one warning) on failure."""
    try:
        with open(path, "r", encoding="utf-8") as config_file:
            data = yaml.safe_load(config_file)
    except (OSError, ValueError, yaml.YAMLError):
        logger.warning("Could not read the config to find the server URL")
        return None
    settings = data.get("settings") if isinstance(data, dict) else None
    value = settings.get("server_url") if isinstance(settings, dict) else None
    return value if isinstance(value, str) else None


def _header_values(scope: Scope, name: bytes) -> list[str]:
    """Return every value of a request header, decoded as latin-1."""
    return [v.decode("latin-1") for k, v in scope["headers"] if k == name]


def _forbid_framing(send: Send) -> Send:
    """Wrap `send` so every response start carries the anti-framing headers."""

    async def guarded(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            kept = [
                (k, v)
                for k, v in message.get("headers", [])
                if k.lower() not in _REPLACED_HEADERS
            ]
            message = {**message, "headers": kept + list(FRAME_HEADERS)}
        await send(message)

    return guarded


class RequestGuardMiddleware:
    """Pure ASGI middleware that applies the Host and Origin rules."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap `app`."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Refuse the request with a fixed JSON message or pass it on."""
        send = _forbid_framing(send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        server_url = read_server_url()
        names = allowed_host_names(server_url, os.environ.get("ALLOWED_HOSTS"))
        hosts = _header_values(scope, b"host")
        host = hosts[0] if len(hosts) == 1 else None
        if not is_host_allowed(host, names):
            await self._refuse(400, HOST_REFUSED, scope, receive, send)
            return
        origins = _header_values(scope, b"origin")
        if is_state_changing(scope["method"]) and origins:
            allowed = len(origins) == 1 and is_origin_allowed(
                origins[0], host, server_url, os.environ.get("ALLOWED_HOSTS")
            )
            if not allowed:
                await self._refuse(403, ORIGIN_REFUSED, scope, receive, send)
                return
        await self._run_app(scope, receive, send)

    async def _run_app(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Run the app; turn an unhandled error into a fixed 500 we control.

        Starlette's own error middleware sits outside this one, so its 500 would
        carry neither the framing headers nor our fixed body. The error is logged
        with secret values redacted. If the response already started, it is
        re-raised: nothing can be sent any more.
        """
        started = False

        async def tracking_send(message: MutableMapping[str, Any]) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as error:
            if started:
                raise
            detail = "".join(traceback.format_exception(error))
            logger.error(
                "Unhandled error on %s %r:\n%s",
                scope["method"],
                scope["path"],
                secrets_store.redact(detail),
            )
            await self._refuse(500, INTERNAL_ERROR, scope, receive, send)

    @staticmethod
    async def _refuse(
        status: int, detail: str, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Send a fixed JSON refusal that never includes request data."""
        await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)
