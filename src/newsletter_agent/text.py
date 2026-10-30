"""Text cleanup for feeds, prompts and Claude's output."""

from __future__ import annotations

import html
import re
from typing import Any

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_SCHEME_URL = re.compile(r"(?i)\b(?:https?|ftp)://\S+")
_BARE_DOMAIN = re.compile(r"\b(?:[\w-]+\.)+[^\W\d_][\w-]{1,62}\b")
_BARE_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_DOTS = str.maketrans({"\u3002": ".", "\uff0e": ".", "\uff61": ".", "\u2024": ".", "\ufe52": "."})
_HIDDEN = r"[\s\u200b-\u200d\u2060\ufeff]*"
_FENCED_TAG = re.compile(rf"(?i)<({_HIDDEN}/?{_HIDDEN})(item|feedback)")


def storable(value: str) -> str:
    """The text without NUL characters, and with lone surrogates replaced, both of which Postgres
    refuses: in a text column, and as \u0000 in a jsonb one.
    """
    return value.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")


def storable_json(value: Any) -> Any:
    """A JSON value with every string in it made storable."""
    if isinstance(value, str):
        return storable(value)
    if isinstance(value, list):
        return [storable_json(v) for v in value]
    if isinstance(value, dict):
        return {storable(str(k)): storable_json(v) for k, v in value.items()}
    return value


def plain(value: str | None, limit: int = 2000) -> str | None:
    """Strip tags, entities and NUL characters and collapse whitespace; None for empty input."""
    if not value:
        return None
    cleaned = _SPACE.sub(" ", html.unescape(_TAG.sub(" ", value)).replace("\x00", "")).strip()
    return cleaned[:limit] or None


def defang(value: str) -> str:
    """Remove URLs and break up every domain-like word, such as `example.com` or `Node.js`, and
    every IPv4 address, so text from Claude or a web page can't become a clickable link in an
    email. Look-alike dots count as dots.
    """
    without_links = _SCHEME_URL.sub("[link removed]", value.translate(_DOTS))
    broken = _BARE_DOMAIN.sub(lambda m: m.group(0).replace(".", "[.]"), without_links)
    return _BARE_IP.sub(lambda m: m.group(0).replace(".", "[.]"), broken)


def fence(value: str) -> str:
    """Escape item and feedback tags, so fetched text can't open or close either in a prompt,
    including when spaces or zero-width characters are put inside the tag.
    """
    return _FENCED_TAG.sub(r"&lt;\1\2", value)
