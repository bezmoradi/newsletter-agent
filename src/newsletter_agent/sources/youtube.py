"""A YouTube channel's Atom feed."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from newsletter_agent.sources import feed
from newsletter_agent.sources.base import Collected, SourceState, changed, get_if_changed, unchanged
from newsletter_agent.urls import watch_url, youtube_video_id

if TYPE_CHECKING:
    from datetime import datetime

    from newsletter_agent.settings import Settings, SourceConfig

FEED_URL = "https://www.youtube.com/feeds/videos.xml"


def collect(
    source: SourceConfig, state: SourceState, settings: Settings, deadline: datetime
) -> Collected:
    assert source.channel_id is not None
    response = get_if_changed(FEED_URL, state, {"channel_id": source.channel_id})
    if response is None:
        return unchanged(state)
    candidates = []
    for candidate in feed.parse(response.body):
        video_id = youtube_video_id(candidate.url)
        if video_id:
            candidates.append(replace(candidate, external_id=video_id, url=watch_url(video_id)))
    return changed(response, candidates)
