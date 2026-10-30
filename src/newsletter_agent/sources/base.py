"""What collectors receive and return."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from newsletter_agent import http

if TYPE_CHECKING:
    from newsletter_agent.llm import Structured
    from newsletter_agent.settings import Settings, SourceConfig


@dataclass(frozen=True)
class Candidate:
    external_id: str
    url: str
    title: str | None = None
    published_at: datetime | None = None
    summary: str | None = None


@dataclass(frozen=True)
class SourceState:
    etag: str | None = None
    last_modified: str | None = None
    last_success_at: datetime | None = None


@dataclass(frozen=True)
class Collected:
    """What one source returned. recent_only means the source searched only recent pages, so its
    candidates count as new even on its first sight; claude holds the Claude calls it made.
    """

    candidates: tuple[Candidate, ...] = ()
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None
    recent_only: bool = False
    claude: tuple[Structured, ...] = ()


type Collector = Callable[[SourceConfig, SourceState, Settings, float], Collected]


class DeadlinePassed(Exception):
    """The run's deadline passed before a source finished, so the source fails rather than
    record a success for work it skipped.
    """


def past(deadline: float) -> bool:
    """True once the run's deadline, a time.monotonic() value, has passed."""
    return time.monotonic() >= deadline


def get_if_changed(
    url: str, state: SourceState, params: dict[str, str | int] | None = None
) -> http.Response | None:
    """GET with the source's stored ETag and Last-Modified; None when the server answers that
    nothing changed.
    """
    response = http.get(url, params=params, etag=state.etag, last_modified=state.last_modified)
    return None if response.not_modified else response


def unchanged(state: SourceState) -> Collected:
    """What a source returns when nothing changed: it keeps its stored ETag and Last-Modified."""
    return Collected(not_modified=True, etag=state.etag, last_modified=state.last_modified)


def changed(
    response: http.Response, candidates: Iterable[Candidate], *, keep_validators: bool = True
) -> Collected:
    """The candidates, with the response's ETag and Last-Modified for the next request."""
    return Collected(
        candidates=tuple(candidates),
        etag=response.headers.get("ETag") if keep_validators else None,
        last_modified=response.headers.get("Last-Modified") if keep_validators else None,
    )
