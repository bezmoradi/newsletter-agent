"""Web search through Claude's WebSearch tool: the only Claude session that has a tool."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from newsletter_agent import llm, prompts
from newsletter_agent.http import FetchError, check_url, describe, https
from newsletter_agent.sources.base import Candidate, Collected, SourceState, past
from newsletter_agent.sources.feed import parse_date
from newsletter_agent.text import plain
from newsletter_agent.urls import MAX_URL_LENGTH, canonical_url, is_web_url

if TYPE_CHECKING:
    from newsletter_agent.settings import Settings, SourceConfig

log = logging.getLogger(__name__)

PROMPT_VERSION = "search_v1"
FIRST_WINDOW_DAYS = 2
MAX_WINDOW_DAYS = 7
MAX_RESULTS = 10
TIMEOUT_SECONDS = 300

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "maxLength": MAX_URL_LENGTH},
                    "title": {"type": "string", "maxLength": 300},
                    "published": {"type": "string", "maxLength": 10},
                },
                "required": ["url", "title", "published"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def collect(
    source: SourceConfig, state: SourceState, settings: Settings, deadline: float
) -> Collected:
    """One Claude session with WebSearch per search term, starting none once the run's deadline
    has passed. A term whose output is invalid is skipped, but when every term's output is invalid
    the source fails with ClaudeOutputError; a sign-in or availability failure stops the source and
    is raised. Claude's links are kept only if they are plain http or https URLs whose host
    resolves to public addresses; the enrich step fetches them, which checks again.
    """
    days = _window_days(state.last_success_at)
    found: dict[str, Candidate] = {}
    calls: list[llm.Structured] = []
    valid = 0
    for term in source.queries:
        if past(deadline):
            log.warning("web search stopped at the run deadline", extra={"fields": {"term": term}})
            break
        try:
            result, candidates = search(term, days, settings.search_model)
        except llm.ClaudeOutputError as e:
            calls.extend(e.spent)
            log.warning(
                "web search term skipped", extra={"fields": {"term": term, "error": str(e)}}
            )
            continue
        valid += 1
        calls.append(result)
        for candidate in candidates:
            found.setdefault(candidate.external_id, candidate)
    else:
        if not valid:
            raise llm.ClaudeOutputError(
                f"all {len(source.queries)} web search terms returned invalid output", tuple(calls)
            )
    return Collected(candidates=tuple(found.values()), recent_only=True, claude=tuple(calls))


def search(term: str, days: int, model: str) -> tuple[llm.Structured, list[Candidate]]:
    """One Claude session with WebSearch. Returns the call and up to MAX_RESULTS links from it,
    keeping only plain http and https URLs on public hosts. Raises the llm errors.
    """
    prompt = prompts.render(PROMPT_VERSION, term=term, days=str(days), max_results=str(MAX_RESULTS))
    result = llm.structured(prompt, SCHEMA, model, timeout=TIMEOUT_SECONDS, web_search=True)
    output = result.output if isinstance(result.output, dict) else {}
    entries = output.get("results")
    candidates = []
    for entry in entries[:MAX_RESULTS] if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        url = _page_url(entry.get("url"))
        if url is not None:
            candidates.append(
                Candidate(
                    external_id=canonical_url(url),
                    url=url,
                    title=plain(entry.get("title"), 500)
                    if isinstance(entry.get("title"), str)
                    else None,
                    published_at=_published(entry.get("published")),
                )
            )
    return result, candidates


def _window_days(last_success_at: datetime | None) -> int:
    if last_success_at is None:
        return FIRST_WINDOW_DAYS
    elapsed = datetime.now(UTC) - last_success_at
    return min(MAX_WINDOW_DAYS, max(1, math.ceil(elapsed / timedelta(days=1))))


def _page_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if len(url) > MAX_URL_LENGTH or not is_web_url(url):
        return None
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        return None
    try:
        check_url(https(url))
    except FetchError as e:
        log.info(
            "web search link dropped", extra={"fields": {"url": describe(url), "error": str(e)}}
        )
        return None
    return url


def _published(value: Any) -> datetime | None:
    parsed = parse_date(value) if isinstance(value, str) else None
    return parsed if parsed and parsed <= datetime.now(UTC) + timedelta(days=1) else None
