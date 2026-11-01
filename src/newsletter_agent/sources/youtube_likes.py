"""The owner's liked YouTube videos, read through the YouTube Data API with the owner's sign-in.
Not a collector: steps.read_likes stores them as ratings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from newsletter_agent import http
from newsletter_agent.sources.base import Candidate, DeadlinePassed, past
from newsletter_agent.sources.feed import parse_date
from newsletter_agent.text import plain
from newsletter_agent.urls import is_channel_id, is_video_id, watch_url

PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
LIKES_PLAYLIST = "LL"
PER_PAGE = 50
MAX_PAGES = 10


@dataclass(frozen=True)
class Like:
    video: Candidate
    liked_at: datetime


def recent(access_token: str, since: datetime, deadline: float) -> list[Like]:
    """The videos liked since `since`, newest first, from at most MAX_PAGES pages of PER_PAGE,
    each with its title, description and channel; a video deleted or made private since, which
    has no channel, is left out. The likes list is YouTube's "LL" playlist,
    whose items are dated when they were liked. Raises http.FetchError, or DeadlinePassed when
    the run's deadline passes before the last page.
    """
    likes: list[Like] = []
    page_token = ""
    for page in range(MAX_PAGES):
        if page and past(deadline):
            raise DeadlinePassed("the run deadline passed before every page of likes was read")
        params: dict[str, str | int] = {
            "part": "snippet,contentDetails",
            "playlistId": LIKES_PLAYLIST,
            "maxResults": PER_PAGE,
        }
        if page_token:
            params["pageToken"] = page_token
        response = http.get(
            PLAYLIST_ITEMS_URL, params=params, headers={"Authorization": f"Bearer {access_token}"}
        )
        data = json.loads(response.body)
        for entry in data.get("items", []):
            like = _like(entry)
            if like is None:
                continue
            if like.liked_at < since:
                return likes
            likes.append(like)
        page_token = data.get("nextPageToken") or ""
        if not page_token:
            break
    return likes


def _like(entry: object) -> Like | None:
    snippet = entry.get("snippet") if isinstance(entry, dict) else None
    if not isinstance(snippet, dict):
        return None
    video_id = (snippet.get("resourceId") or {}).get("videoId")
    liked_at = parse_date(snippet.get("publishedAt"))
    if not is_video_id(video_id) or liked_at is None:
        return None
    channel = snippet.get("videoOwnerChannelId")
    if not is_channel_id(channel):
        return None
    details = entry.get("contentDetails") if isinstance(entry, dict) else None
    published = (details or {}).get("videoPublishedAt") if isinstance(details, dict) else None
    return Like(
        video=Candidate(
            external_id=video_id,
            url=watch_url(video_id),
            title=plain(snippet.get("title"), 500),
            published_at=parse_date(published),
            summary=plain(snippet.get("description")),
            publisher=channel,
        ),
        liked_at=liked_at,
    )
