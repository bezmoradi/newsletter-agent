"""RSS 1.0, RSS 2.0 and Atom feeds, parsed with xml.etree."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

from newsletter_agent.sources.base import (
    Candidate,
    Collected,
    SourceState,
    changed,
    get_if_changed,
    unchanged,
)
from newsletter_agent.text import plain
from newsletter_agent.urls import is_web_url

if TYPE_CHECKING:
    from newsletter_agent.settings import Settings, SourceConfig

ID_TAGS = ("videoId", "guid", "id")
DATE_TAGS = ("published", "pubDate", "date", "updated", "issued", "modified")
SUMMARY_TAGS = ("summary", "description", "encoded", "content")


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse(body: bytes, base_url: str = "") -> list[Candidate]:
    """Candidates from an RSS 1.0, RSS 2.0 or Atom document. Raises on malformed XML or a root that
    isn't a feed.
    """
    root = ET.fromstring(body.lstrip())
    if local(root.tag) not in ("rss", "feed", "RDF"):
        raise ValueError(f"not an RSS or Atom feed: root element {local(root.tag)!r}")
    candidates = []
    for entry in root.iter():
        if local(entry.tag) not in ("item", "entry"):
            continue
        link = _link(entry, base_url)
        if not is_web_url(link):
            continue
        assert link is not None
        candidates.append(
            Candidate(
                external_id=(_first_text(entry, ID_TAGS) or link)[:500],
                url=link,
                title=plain(_first_text(entry, ("title",)), 500),
                published_at=parse_date(_first_text(entry, DATE_TAGS)),
                summary=plain(_first_text(entry, SUMMARY_TAGS, deep=True)),
            )
        )
    return candidates


def collect(
    source: SourceConfig, state: SourceState, settings: Settings, deadline: datetime
) -> Collected:
    assert source.url is not None
    response = get_if_changed(source.url, state)
    if response is None:
        return unchanged(state)
    return changed(
        response,
        (c for c in parse(response.body, response.url) if source.matches(urlsplit(c.url).path)),
    )


def _text(element: ET.Element) -> str:
    return "".join(element.itertext()).strip()


def _first_text(entry: ET.Element, names: tuple[str, ...], *, deep: bool = False) -> str | None:
    elements = list(entry.iter() if deep else entry)
    for name in names:
        for element in elements:
            if element is not entry and local(element.tag) == name:
                value = _text(element)
                if value:
                    return value
    return None


def _link(entry: ET.Element, base_url: str) -> str | None:
    fallback = None
    for element in entry:
        if local(element.tag) != "link":
            continue
        href = element.get("href")
        if href is None:
            value = _text(element)
            if value:
                return urljoin(base_url, value)
            continue
        rel = element.get("rel", "alternate")
        if rel == "alternate":
            return urljoin(base_url, href)
        fallback = fallback or urljoin(base_url, href)
    return fallback


def parse_date(value: str | None) -> datetime | None:
    """An ISO 8601 or RFC 822 date, in UTC when it names no zone; None when it doesn't parse."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
