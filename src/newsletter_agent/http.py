"""Outgoing HTTP over https only, with timeouts, a size cap and checks against non-public
addresses. Each connection goes to an address the check approved, so a second DNS answer can't
send it elsewhere.
"""

from __future__ import annotations

import http.client
import ipaddress
import signal
import socket
import urllib.error
import urllib.request
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import Message
from functools import partial
from types import FrameType
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
SOCKET_TIMEOUT = 30
REQUEST_DEADLINE = 90
CHUNK = 64 * 1024
USER_AGENT = "newsletter-agent/0.1 (personal use)"
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


class FetchError(Exception):
    pass


class BlockedAddress(FetchError):
    pass


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    headers: Message
    body: bytes

    @property
    def not_modified(self) -> bool:
        return self.status == 304


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def _connect_any(
    addresses: tuple[str, ...],
    address: tuple[str, int],
    timeout: Any,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    error: OSError = OSError("no address to connect to")
    for ip in addresses:
        try:
            return socket.create_connection((ip, address[1]), timeout, source_address)
        except OSError as e:
            error = e
    raise error


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, *, addresses: tuple[str, ...], **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._create_connection = partial(_connect_any, addresses)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, addresses: tuple[str, ...]) -> None:
        super().__init__()
        self._addresses = addresses

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        connection: Any = partial(_PinnedHTTPSConnection, addresses=self._addresses)
        return self.do_open(connection, req)


def describe(url: str) -> str:
    """The URL without its query string, so it is safe to log."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(malformed URL)"
    return f"{parts.scheme}://{parts.hostname or ''}{parts.path}"


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def https(url: str) -> str:
    """The URL with an http scheme changed to https, dropping an explicit port 80."""
    parts = urlsplit(url)
    if parts.scheme != "http":
        return url
    netloc = parts.netloc
    if parts.port == 80:
        netloc = netloc.rsplit(":", 1)[0]
    return urlunsplit(("https", netloc, parts.path, parts.query, parts.fragment))


def check_url(url: str) -> tuple[str, ...]:
    """Return the addresses the URL's host resolves to. Raise BlockedAddress unless the URL is
    well formed, http or https, and every one of those addresses is globally routable.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise BlockedAddress(f"malformed URL: {describe(url)}") from None
    if parts.scheme not in ("http", "https"):
        raise BlockedAddress(f"scheme {parts.scheme!r} is not allowed: {describe(url)}")
    if not host:
        raise BlockedAddress(f"no host in {describe(url)}")
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except UnicodeError:
            raise BlockedAddress(f"malformed host in {describe(url)}") from None
        except OSError as e:
            raise FetchError(f"cannot resolve {host}: {e}") from None
        addresses = sorted({str(info[4][0]).split("%", 1)[0] for info in infos})
    if not addresses or not all(is_public(a) for a in addresses):
        raise BlockedAddress(f"{host} resolves to a non-public address")
    return tuple(addresses)


@contextmanager
def deadline(seconds: float, message: str) -> Iterator[None]:
    """Raise FetchError(message) if the block runs longer than seconds. It uses SIGALRM, so call
    it from the main thread, and not inside another deadline.
    """

    def expire(_signum: int, _frame: FrameType | None) -> None:
        raise FetchError(message)

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def get(
    url: str,
    *,
    params: dict[str, str | int] | None = None,
    headers: dict[str, str] | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
) -> Response:
    """GET over https, with address checks on every hop, manual redirects, a 30 s socket timeout,
    a 90 s deadline for the whole request and a 5 MB cap. An http URL or redirect is fetched as
    https; a redirect to another host drops the caller's headers. Returns a 304 response when the
    ETag or Last-Modified still matches; raises FetchError on any other non-2xx status, network
    error or the deadline.
    """
    base_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    request_headers = {**base_headers, **(headers or {})}
    if etag:
        request_headers["If-None-Match"] = etag
    if last_modified:
        request_headers["If-Modified-Since"] = last_modified
    current = url
    if params:
        current += ("&" if urlsplit(url).query else "?") + urlencode(params)
    with deadline(
        REQUEST_DEADLINE, f"no complete response within {REQUEST_DEADLINE} s from {describe(url)}"
    ):
        return _follow(url, current, request_headers, base_headers)


def _follow(
    url: str,
    current: str,
    request_headers: dict[str, str],
    base_headers: dict[str, str],
) -> Response:
    for _ in range(MAX_REDIRECTS + 1):
        current = https(current)
        addresses = check_url(current)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect, _PinnedHTTPSHandler(addresses)
        )
        request = urllib.request.Request(current, headers=request_headers, method="GET")
        try:
            with opener.open(request, timeout=SOCKET_TIMEOUT) as response:
                body = _read_capped(response, current)
                return Response(
                    response.geturl(),
                    response.status,
                    response.headers,
                    _decode(body, response.headers, current),
                )
        except urllib.error.HTTPError as e:
            with e:
                if e.code in REDIRECT_CODES and e.headers.get("Location"):
                    target = urljoin(current, e.headers["Location"])
                    if urlsplit(target).hostname != urlsplit(current).hostname:
                        request_headers = base_headers
                    current = target
                    continue
                if e.code == 304:
                    return Response(current, 304, e.headers, b"")
                raise FetchError(f"HTTP {e.code} from {describe(current)}") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
            raise FetchError(f"{type(e).__name__} fetching {describe(current)}: {e}") from None
    raise FetchError(f"more than {MAX_REDIRECTS} redirects from {describe(url)}")


def _read_capped(response: http.client.HTTPResponse, url: str) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > MAX_BYTES:
        raise FetchError(f"body over {MAX_BYTES} bytes from {describe(url)}")
    chunks = []
    size = 0
    while chunk := response.read(CHUNK):
        size += len(chunk)
        if size > MAX_BYTES:
            raise FetchError(f"body over {MAX_BYTES} bytes from {describe(url)}")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode(body: bytes, headers: Message, url: str) -> bytes:
    if (headers.get("Content-Encoding") or "").strip().lower() != "gzip":
        return body
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        data = inflater.decompress(body, MAX_BYTES + 1)
    except zlib.error as e:
        raise FetchError(f"bad gzip body from {describe(url)}: {e}") from None
    if len(data) > MAX_BYTES or inflater.unconsumed_tail:
        raise FetchError(f"body over {MAX_BYTES} bytes once unzipped from {describe(url)}")
    return data
