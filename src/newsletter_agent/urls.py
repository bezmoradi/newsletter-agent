"""URL helpers: canonical URLs for deduplication and YouTube video IDs."""

from __future__ import annotations

import re
from typing import TypeGuard
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"})
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
MAX_URL_LENGTH = 2000


def is_video_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _VIDEO_ID.fullmatch(value) is not None


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def youtube_video_id(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    candidate = None
    if host in _YOUTUBE_HOSTS:
        if parts.path == "/watch":
            candidate = dict(parse_qsl(parts.query)).get("v")
        elif parts.path.startswith("/shorts/"):
            candidate = parts.path.split("/")[2]
    elif host == "youtu.be":
        candidate = parts.path.lstrip("/").split("/")[0]
    return candidate if is_video_id(candidate) else None


def canonical_url(url: str) -> str:
    """The URL used to deduplicate items across sources. YouTube links become watch URLs; the
    fragment, utm_* and source=rss parameters and a trailing slash are dropped. Raises ValueError
    for a malformed port; is_web_url rejects such URLs first.
    """
    video_id = youtube_video_id(url)
    if video_id:
        return watch_url(video_id)
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    netloc = host if parts.port in (None, 80, 443) else f"{host}:{parts.port}"
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
        and not (k.lower() == "source" and v.lower().startswith("rss"))
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(query), ""))


def is_web_url(url: str | None) -> bool:
    """True for an http or https URL with a host and a valid port, if it has one."""
    if not url:
        return False
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def domain(url: str) -> str:
    """The URL's host without www., in ASCII, so an international look-alike of a familiar
    domain shows in its xn-- form.
    """
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host
