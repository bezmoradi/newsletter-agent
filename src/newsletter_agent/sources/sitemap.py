"""Sitemaps and sitemap indexes, filtered by each source's include rules."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from newsletter_agent import http
from newsletter_agent.sources.base import (
    Candidate,
    Collected,
    SourceState,
    changed,
    get_if_changed,
    unchanged,
)
from newsletter_agent.sources.feed import local
from newsletter_agent.urls import canonical_url, is_web_url

if TYPE_CHECKING:
    from datetime import datetime

    from newsletter_agent.settings import Settings, SourceConfig

MAX_CHILD_SITEMAPS = 10


def collect(
    source: SourceConfig, state: SourceState, settings: Settings, deadline: datetime
) -> Collected:
    """Every URL whose path matches an include rule, with no dates: sitemap dates aren't trusted,
    so a sitemap's first sight is all baseline. An index's ETag isn't kept, because a child can
    change while the index stays the same.
    """
    assert source.url is not None
    response = get_if_changed(source.url, state)
    if response is None:
        return unchanged(state)
    root = ET.fromstring(response.body)
    is_index = local(root.tag) == "sitemapindex"
    if is_index:
        host = urlsplit(source.url).hostname
        children = [u for u in _locs(root) if urlsplit(u).hostname == host][:MAX_CHILD_SITEMAPS]
        urls = [u for child in children for u in _locs(ET.fromstring(http.get(child).body))]
    else:
        urls = _locs(root)
    seen: set[str] = set()
    candidates = []
    for url in urls:
        if not is_web_url(url) or not source.matches(urlsplit(url).path):
            continue
        key = canonical_url(url)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(Candidate(external_id=key, url=url))
    return changed(response, candidates, keep_validators=not is_index)


def _locs(root: ET.Element) -> list[str]:
    return [
        loc.text.strip()
        for entry in root
        if local(entry.tag) in ("url", "sitemap")
        for loc in entry
        if local(loc.tag) == "loc" and loc.text
    ]
