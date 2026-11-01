"""The collector for each source type in settings.SOURCE_TYPES, except the rating types, which
steps.read_likes reads.
"""

from newsletter_agent.settings import RATING_TYPES, SOURCE_TYPES
from newsletter_agent.sources import feed, sitemap, web_search, youtube, youtube_search
from newsletter_agent.sources.base import Collector

COLLECTORS: dict[str, Collector] = {
    "feed": feed.collect,
    "sitemap": sitemap.collect,
    "youtube": youtube.collect,
    "youtube_search": youtube_search.collect,
    "web_search": web_search.collect,
}
assert set(COLLECTORS) == set(SOURCE_TYPES) - set(RATING_TYPES), (
    "every source type except the rating types needs exactly one collector"
)
