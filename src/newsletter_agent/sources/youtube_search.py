"""YouTube topic search through the YouTube Data API."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from newsletter_agent import http
from newsletter_agent.sources.base import Candidate, Collected, DeadlinePassed, SourceState, past
from newsletter_agent.sources.feed import parse_date
from newsletter_agent.text import plain
from newsletter_agent.urls import is_channel_id, is_video_id, watch_url

if TYPE_CHECKING:
    from newsletter_agent.settings import Settings, SourceConfig

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
PROBE_CHANNEL = "UCrDwWp7EBBv4NwvScIpBDOA"
FIRST_WINDOW = timedelta(hours=48)
INDEX_LAG = timedelta(hours=12)
MAX_SHORT = timedelta(minutes=3)
VIDEOS_PER_CALL = 50
_DURATION_RE = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")

log = logging.getLogger(__name__)


def collect(
    source: SourceConfig, state: SourceState, settings: Settings, deadline: float
) -> Collected:
    """One search.list call per query for videos published in the window, then one videos.list
    call per 50 videos found, which drops videos of 3 minutes or less, YouTube's limit for a
    Short, and videos under both the likes and the comments bar. The window ends
    youtube_search_min_age_days before now and starts 12 hours before the end of the last
    successful window, because YouTube indexes some videos hours after they are published; its
    end is returned as last_modified, so the next window continues from it whatever the age
    setting is then. A window that would end before it starts makes no call. Any failed call, or
    the run's deadline passing before the last call (DeadlinePassed), fails the whole source, so
    the window doesn't move past results it missed.
    """
    if not settings.youtube_api_key:
        raise RuntimeError("YOUTUBE_API_KEY is not set")
    until = datetime.now(UTC) - timedelta(days=settings.youtube_search_min_age_days)
    searched_until = _searched_until(state)
    since = until - FIRST_WINDOW if searched_until is None else searched_until - INDEX_LAG
    if since >= until:
        return Collected(last_modified=_stamp(searched_until or until))
    window = {"publishedAfter": _stamp(since)}
    if settings.youtube_search_min_age_days:
        window["publishedBefore"] = _stamp(until)
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
                **window,
            },
            headers={"X-Goog-Api-Key": settings.youtube_api_key},
        )
        for entry in json.loads(response.body).get("items", []):
            video_id = (entry.get("id") or {}).get("videoId")
            snippet = entry.get("snippet") or {}
            if not is_video_id(video_id):
                continue
            if video_id in found:
                found[video_id] = _found_by(found[video_id], query)
                continue
            channel_id = snippet.get("channelId")
            found[video_id] = Candidate(
                external_id=video_id,
                url=watch_url(video_id),
                title=plain(snippet.get("title"), 500),
                published_at=parse_date(snippet.get("publishedAt")),
                summary=plain(snippet.get("description")),
                search_terms=(query,),
                publisher=channel_id if is_channel_id(channel_id) else None,
            )
    kept = _kept(list(found), settings, deadline)
    return Collected(
        candidates=tuple(c for video_id, c in found.items() if video_id in kept),
        last_modified=_stamp(until),
    )


def _searched_until(state: SourceState) -> datetime | None:
    """Where the last successful window ended: its stored end, or else, for a source last
    searched before windows were stored, the time of that search.
    """
    if state.last_modified:
        try:
            return datetime.fromisoformat(state.last_modified).astimezone(UTC)
        except ValueError:
            pass
    return state.last_success_at


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _found_by(candidate: Candidate, term: str) -> Candidate:
    if term in candidate.search_terms:
        return candidate
    return replace(candidate, search_terms=(*candidate.search_terms, term))


def _kept(video_ids: list[str], settings: Settings, deadline: float) -> set[str]:
    """The IDs among video_ids of videos longer than MAX_SHORT that reach the likes bar or the
    comments bar; a count YouTube doesn't return counts as reaching its bar. A video that
    videos.list doesn't return, or whose length doesn't parse, is left out. Each video dropped
    for its counts is logged with them.
    """
    kept: set[str] = set()
    short = 0
    for start in range(0, len(video_ids), VIDEOS_PER_CALL):
        if past(deadline):
            raise DeadlinePassed("the run deadline passed before every video's length was read")
        response = http.get(
            VIDEOS_URL,
            params={
                "part": "contentDetails,statistics",
                "id": ",".join(video_ids[start : start + VIDEOS_PER_CALL]),
                "maxResults": VIDEOS_PER_CALL,
            },
            headers={"X-Goog-Api-Key": settings.youtube_api_key},
        )
        for entry in json.loads(response.body).get("items", []):
            video_id = entry.get("id")
            length = _length((entry.get("contentDetails") or {}).get("duration"))
            if not is_video_id(video_id) or length is None or length <= MAX_SHORT:
                short += 1
                continue
            statistics = entry.get("statistics") or {}
            likes = _count(statistics.get("likeCount"))
            comments = _count(statistics.get("commentCount"))
            if _under(likes, settings.youtube_search_min_likes) and _under(
                comments, settings.youtube_search_min_comments
            ):
                log.info(
                    "unpopular video skipped",
                    extra={"fields": {"video": video_id, "likes": likes, "comments": comments}},
                )
                continue
            kept.add(video_id)
    if short:
        log.info("short videos skipped", extra={"fields": {"skipped": short}})
    return kept


def _count(value: object) -> int | None:
    try:
        return int(value) if isinstance(value, str | int) else None
    except ValueError:
        return None


def _under(count: int | None, bar: int) -> bool:
    return count is not None and count < bar


def _length(duration: object) -> timedelta | None:
    match = _DURATION_RE.fullmatch(duration) if isinstance(duration, str) else None
    if match is None:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def check_key(api_key: str) -> None:
    """Confirm the API key works with one call that costs 1 quota unit."""
    http.get(
        CHANNELS_URL,
        params={"part": "id", "id": PROBE_CHANNEL},
        headers={"X-Goog-Api-Key": api_key},
    )
