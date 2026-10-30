"""Article text and YouTube transcripts for collected items."""

from __future__ import annotations

from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from youtube_transcript_api import YouTubeTranscriptApi

from newsletter_agent import http
from newsletter_agent.extract import decode, extract

MAX_TEXT = 100_000
TRANSCRIPT_TIMEOUT = 30
PARSE_DEADLINE = 30
PAGE_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})


@dataclass(frozen=True)
class Content:
    text: str | None
    title: str | None = None


class _TimeoutAdapter(HTTPAdapter):
    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: float | tuple[float | None, float | None] | None = None,
        verify: bool | str = True,
        cert: str | tuple[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> requests.Response:
        return super().send(
            request,
            stream=stream,
            timeout=timeout or TRANSCRIPT_TIMEOUT,
            verify=verify,
            cert=cert,
            proxies=proxies,
        )


def transcript(video_id: str) -> Content:
    """The English transcript, with a 30-second timeout per request. Raises when there is none."""
    with requests.Session() as session:
        adapter = _TimeoutAdapter()
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        fetched = YouTubeTranscriptApi(http_client=session).fetch(video_id, languages=["en"])
        text = " ".join(snippet.text for snippet in fetched).strip()
    return Content(text=_storable(text, MAX_TEXT))


def article(url: str) -> Content:
    """The page's main text and title. Raises ValueError when the response isn't a web page,
    such as a PDF or an image, and FetchError when parsing takes over 30 seconds.
    """
    response = http.get(url)
    if response.headers.get("Content-Type"):
        content_type = response.headers.get_content_type()
        if content_type not in PAGE_TYPES:
            raise ValueError(f"not a web page: {content_type}")
    with http.deadline(PARSE_DEADLINE, f"over {PARSE_DEADLINE} s to parse {http.describe(url)}"):
        text, title = extract(decode(response.body, response.headers))
    return Content(text=_storable(text, MAX_TEXT), title=_storable(title, 500))


def _storable(value: str | None, limit: int) -> str | None:
    return (value or "").replace("\x00", "")[:limit] or None
