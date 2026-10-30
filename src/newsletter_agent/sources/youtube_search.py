"""YouTube topic search through the YouTube Data API."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from newsletter_agent import http
from newsletter_agent.sources.base import Candidate, Collected, DeadlinePassed, SourceState, past
from newsletter_agent.sources.feed import parse_date
from newsletter_agent.text import plain
from newsletter_agent.urls import is_video_id, watch_url

if TYPE_CHECKING:
    from newsletter_agent.settings import Settings, SourceConfig

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
PROBE_CHANNEL = "UCrDwWp7EBBv4NwvScIpBDOA"
FIRST_WINDOW = timedelta(hours=48)
INDEX_LAG = timedelta(hours=12)


def collect(
    source: SourceConfig, state: SourceState, settings: Settings, deadline: float
) -> Collected:
    """One search.list call per query. Any failed call, or the run's deadline passing before the
    last query (DeadlinePassed), fails the whole source, so publishedAfter doesn't move past
    results it missed. Each search reaches 12 hours before the last success, because YouTube
    indexes some videos hours after they are published.
    """
    if not settings.youtube_api_key:
        raise RuntimeError("YOUTUBE_API_KEY is not set")
    if state.last_success_at is None:
        since = datetime.now(UTC) - FIRST_WINDOW
    else:
        since = state.last_success_at - INDEX_LAG
    found: dict[str, Candidate] = {}
    for query in source.queries:
        if past(deadline):
            raise DeadlinePassed("the run deadline passed before every search term ran")
        response = http.get(
            SEARCH_URL,
            params={
                "part": "snippet",
                "type": "video",
                "q": query,
                "order": "relevance",
                "relevanceLanguage": "en",
                "maxResults": 25,
                "publishedAfter": since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            headers={"X-Goog-Api-Key": settings.youtube_api_key},
        )
        for entry in json.loads(response.body).get("items", []):
            video_id = (entry.get("id") or {}).get("videoId")
            snippet = entry.get("snippet") or {}
            if not is_video_id(video_id) or video_id in found:
                continue
            found[video_id] = Candidate(
                external_id=video_id,
                url=watch_url(video_id),
                title=plain(snippet.get("title"), 500),
                published_at=parse_date(snippet.get("publishedAt")),
                summary=plain(snippet.get("description")),
            )
    return Collected(candidates=tuple(found.values()))


def check_key(api_key: str) -> None:
    """Confirm the API key works with one call that costs 1 quota unit."""
    http.get(
        CHANNELS_URL,
        params={"part": "id", "id": PROBE_CHANNEL},
        headers={"X-Goog-Api-Key": api_key},
    )
