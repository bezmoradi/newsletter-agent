"""The steps of a run: read ratings, collect, enrich, assess, digest and deliver."""

from __future__ import annotations

import logging
import secrets
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from newsletter_agent import enrich as enrich_mod
from newsletter_agent import llm, mail, prompts, store
from newsletter_agent.settings import MAX_WAIT_DAYS, NewsletterConfig, Settings, SourceConfig
from newsletter_agent.sources import COLLECTORS
from newsletter_agent.sources.base import Collected, DeadlinePassed, SourceState
from newsletter_agent.text import fence
from newsletter_agent.urls import MAX_URL_LENGTH, canonical_url, is_web_url, youtube_video_id

log = logging.getLogger(__name__)

SCORE_PROMPT_VERSION = "score_v3"
DIGEST_PROMPT_VERSION = "digest_v3"
SUMMARY_PROMPT_VERSION = "summary_v1"
CHECK_PROMPT_VERSION = "check_v1"
REVISE_PROMPT_VERSION = "revise_v1"
TRIAGE_PROMPT_VERSION = "triage_v1"
PROFILE_PROMPT_VERSION = "profile_v1"
TRIAGE_MAX_TITLES = 200
PROFILE_MIN_RATINGS = 20
PROFILE_MAX_RATINGS = 100
PROFILE_EVERY = timedelta(days=30)
RATINGS_READ_DAYS = 30
RATINGS_PROMPT_DAYS = 180
RATINGS_PER_SIDE = 5
RATINGS_PER_SOURCE = 2
RESCORE_SHARE = 0.1
NO_RATINGS = "Liked:\n(none yet)\nDisliked:\n(none yet)"
RATED_TITLE_LENGTH = 200
SCORE_BATCH_CHARS = 200_000
MAX_TAKEAWAYS = 4
MAX_MISSING = 3
MAX_ASSESS_ATTEMPTS = 3
DIGEST_SOURCE_CAP = 3
DIGEST_POOL = 30
SENT_TITLE_DAYS = 7
BELOW_THRESHOLD_DAYS = 7
YOUTUBE_TYPES = frozenset({"youtube", "youtube_search"})
CLAUDE_SOURCE_TYPES = frozenset({"web_search"})
MAX_OUTPUT_ERRORS = 4

SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "score": {"type": "integer", "minimum": 0, "maximum": 10},
                    "reason": {"type": "string", "maxLength": 300},
                },
                "required": ["n", "score", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

DIGEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": ["n"],
                "additionalProperties": False,
            },
        },
        "repeats": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["items", "repeats"],
    "additionalProperties": False,
}

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "gist": {"type": "string", "maxLength": 600},
        "takeaways": {
            "type": "array",
            "items": {"type": "string", "maxLength": 600},
            "maxItems": MAX_TAKEAWAYS,
        },
        "highlights": {"type": "string", "maxLength": 400},
    },
    "required": ["gist", "takeaways", "highlights"],
    "additionalProperties": False,
}

CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "unsupported": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string", "maxLength": 600},
                    "why": {"type": "string", "maxLength": 600},
                },
                "required": ["claim", "why"],
                "additionalProperties": False,
            },
        },
        "missing": {
            "type": "array",
            "items": {"type": "string", "maxLength": 600},
            "maxItems": MAX_MISSING,
        },
    },
    "required": ["unsupported", "missing"],
    "additionalProperties": False,
}

TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "score": {"type": "integer", "minimum": 0, "maximum": 10},
                },
                "required": ["n", "score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "change": {"type": "string", "maxLength": 400},
                    "why": {"type": "string", "maxLength": 400},
                },
                "required": ["change", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["suggestions"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class _Choice:
    """The digest call's answer: the chosen candidates in reading order, and the IDs of the
    candidates it left out as repeats.
    """

    chosen: list[store.Row]
    repeats: set[int]


@dataclass
class Run:
    """The state of one run: counts, Claude usage and failure flags. deadline is a
    time.monotonic() value, so a change of the system clock doesn't move it.
    """

    id: int
    deadline: float
    counts: Counter[str] = field(default_factory=Counter)
    usage: dict[str, Any] = field(default_factory=dict)
    claude_stopped: bool = False
    claude_unavailable: bool = False
    claude_auth_error: str | None = None
    ratings_error: str | None = None
    source_errors: list[str] = field(default_factory=list)
    output_errors: int = 0
    degraded: bool = False

    def past_deadline(self) -> bool:
        return time.monotonic() >= self.deadline

    @property
    def all_sources_failed(self) -> bool:
        tried = self.counts["sources_tried"]
        return tried > 0 and self.counts["source_failures"] >= tried

    def record_usage(self, *results: llm.Structured) -> None:
        """Count each Claude session and add its usage to the run's totals, whether or not its
        output was valid.
        """
        for result in results:
            self.counts["claude_calls"] += 1
            totals = self.usage.setdefault(result.model, {})
            for key, value in result.usage.items():
                if isinstance(value, int | float):
                    totals[key] = totals.get(key, 0) + value
            if result.cost_usd is not None:
                totals["cost_usd"] = round(totals.get("cost_usd", 0.0) + result.cost_usd, 6)

    def claude_failed(self, error: llm.ClaudeError) -> None:
        self.record_usage(*error.spent)
        self.claude_stopped = True
        self.degraded = True
        if isinstance(error, llm.ClaudeUnavailable):
            self.claude_unavailable = True
        else:
            self.claude_auth_error = str(error)
        log.warning("claude stopped for this run", extra={"fields": {"error": str(error)}})


def read_ratings(conn: store.Connection, settings: Settings, run: Run) -> None:
    """Store the ratings the owner sent in the last 30 days through the links in digests addressed
    to SMTP_USERNAME. Does nothing when no newsletter goes to SMTP_USERNAME. A failure is recorded
    on the run, which goes on as partial, and shown in the owner's health footer.
    """
    owned = [n.key for n in settings.newsletters if settings.is_owner(n)]
    if not owned:
        return
    since = date.today() - timedelta(days=RATINGS_READ_DAYS)
    try:
        ratings = mail.read_ratings(settings.mail, since)
        added = sum(store.save_rating(conn, rating, owned) for rating in ratings)
        conn.commit()
    except Exception as e:
        store.rollback_quietly(conn)
        run.ratings_error = f"{type(e).__name__}: {e}"[:500]
        run.degraded = True
        log.warning("ratings not read", extra={"fields": {"error": run.ratings_error}})
        return
    run.counts["ratings_new"] += added
    log.info("ratings read", extra={"fields": {"found": len(ratings), "new": added}})


def collect(conn: store.Connection, settings: Settings, run: Run) -> None:
    """Store new items from every enabled source of every newsletter. A failing source, or a
    failure while storing its items, is recorded and skipped; a candidate whose URL is invalid or
    over 2,000 characters is skipped. On a source's first sight, items older than BACKFILL_DAYS,
    and every sitemap entry, are stored as baseline; web search results are always new, because
    the search asked only for recent pages. Once Claude has stopped for the run, sources that
    need Claude are skipped.
    """
    for newsletter in settings.newsletters:
        for source in newsletter.sources:
            if run.past_deadline():
                run.degraded = True
                return
            if run.claude_stopped and source.type in CLAUDE_SOURCE_TYPES:
                run.degraded = True
                continue
            run.counts["sources_tried"] += 1
            _collect_source(conn, settings, run, newsletter, source)


def _collect_source(
    conn: store.Connection,
    settings: Settings,
    run: Run,
    newsletter: NewsletterConfig,
    source: SourceConfig,
) -> None:
    row = store.sync_source(conn, newsletter.key, source)
    conn.commit()
    state = SourceState(row["etag"], row["last_modified"], row["last_success_at"])
    first_sight = row["first_seen_at"] is None
    now = datetime.now(UTC)
    where = {"newsletter": newsletter.key, "source": source.key}
    try:
        collected = COLLECTORS[source.type](source, state, settings, run.deadline)
    except Exception as e:
        if isinstance(e, llm.ClaudeAuthError | llm.ClaudeUnavailable):
            run.claude_failed(e)
        elif isinstance(e, llm.ClaudeOutputError | DeadlinePassed):
            run.record_usage(*e.spent)
        _source_failed(conn, run, row["id"], now, e, where)
        return
    run.record_usage(*collected.claude)
    try:
        added, skipped = _store_candidates(
            conn, settings, newsletter.key, row["id"], collected, first_sight=first_sight, now=now
        )
        store.record_source_success(
            conn,
            row["id"],
            now=now,
            etag=collected.etag,
            last_modified=collected.last_modified,
            new_items=added["new"],
        )
        conn.commit()
    except Exception as e:
        _source_failed(conn, run, row["id"], now, e, where)
        return
    for state_name, count in added.items():
        run.counts[f"items_{state_name}"] += count
    log.info(
        "source collected",
        extra={
            "fields": {
                **where,
                "not_modified": collected.not_modified,
                "candidates": len(collected.candidates),
                "new": added["new"],
                "skipped": skipped,
                "first_sight": first_sight,
            }
        },
    )


def _store_candidates(
    conn: store.Connection,
    settings: Settings,
    newsletter_key: str,
    source_id: int,
    collected: Collected,
    *,
    first_sight: bool,
    now: datetime,
) -> tuple[Counter[str], int]:
    cutoff = now - timedelta(days=settings.backfill_days)
    added: Counter[str] = Counter()
    skipped = 0
    for candidate in collected.candidates:
        if len(candidate.url) > MAX_URL_LENGTH or not is_web_url(candidate.url):
            skipped += 1
            continue
        fresh = collected.recent_only or (
            candidate.published_at is not None and candidate.published_at >= cutoff
        )
        state_name = "baseline" if first_sight and not fresh else "new"
        canonical = canonical_url(candidate.url)
        if store.insert_item(conn, newsletter_key, source_id, candidate, canonical, state_name):
            added[state_name] += 1
    return added, skipped


def _source_failed(
    conn: store.Connection,
    run: Run,
    source_id: int,
    now: datetime,
    error: Exception,
    where: dict[str, str],
) -> None:
    detail = f"{type(error).__name__}: {error}"
    store.rollback_quietly(conn)
    store.record_source_failure(conn, source_id, now=now, error=detail)
    conn.commit()
    run.counts["source_failures"] += 1
    if len(run.source_errors) < 3:
        run.source_errors.append(f"{where['source']}: {detail[:200]}")
    run.degraded = True
    log.warning("source failed", extra={"fields": {**where, "error": detail}})


def output_error_limit(batch_size: int) -> int:
    """How many invalid scoring answers in a row stop Claude for the run: at least
    MAX_OUTPUT_ERRORS, and enough for the retry and the halving to reach a single item.
    """
    return max(MAX_OUTPUT_ERRORS, 2 + batch_size.bit_length())


def shares(total: int, newsletters: int, used: list[int]) -> int:
    """The next newsletter's part of a run-wide cap of total: an even share of what's left, so an
    early newsletter can't take it all, and a later one gets what earlier ones didn't use.
    """
    left = max(0, total - sum(used))
    remaining = newsletters - len(used)
    return -(-left // remaining) if remaining > 0 else 0


def expire(conn: store.Connection, run: Run) -> None:
    """Move items that waited more than MAX_WAIT_DAYS to be fetched or scored back to baseline,
    so a queue that outgrows the run's cap doesn't fill with stale items.
    """
    expired = store.expire_items(conn, MAX_WAIT_DAYS)
    conn.commit()
    if expired:
        run.counts["items_expired"] += expired
        log.info("waiting items expired", extra={"fields": {"items": expired}})


def triage(conn: store.Connection, settings: Settings, run: Run) -> None:
    """Score the titles of new items, with one Claude call per newsletter whose queue holds more
    new items than a run fetches, so fetching and scoring take each source's most promising items
    first. Items keep their turn between sources; invalid output is skipped.
    """
    for newsletter in settings.newsletters:
        if run.claude_stopped or run.past_deadline():
            return
        keys = [s.key for s in newsletter.sources]
        rows: list[store.Row] = []
        if store.new_item_count(conn, newsletter.key, keys) > settings.max_items_per_run:
            rows = store.untriaged_items(conn, newsletter.key, keys, TRIAGE_MAX_TITLES)
        conn.commit()
        if rows:
            _triage_titles(conn, settings, newsletter, run, rows)


def _triage_titles(
    conn: store.Connection,
    settings: Settings,
    newsletter: NewsletterConfig,
    run: Run,
    rows: list[store.Row],
) -> None:
    prompt = prompts.render(
        TRIAGE_PROMPT_VERSION,
        interests=newsletter.interests,
        items="\n\n".join(_item_block(n, row) for n, row in enumerate(rows, 1)),
    )
    try:
        result = llm.structured(prompt, TRIAGE_SCHEMA, settings.score_model)
    except llm.ClaudeOutputError as e:
        run.record_usage(*e.spent)
        log.warning("invalid title scores", extra={"fields": {"error": str(e)}})
        return
    except (llm.ClaudeUnavailable, llm.ClaudeAuthError) as e:
        run.claude_failed(e)
        return
    run.record_usage(result)
    entries = result.output.get("items") if isinstance(result.output, dict) else None
    scored = 0
    for entry in entries if isinstance(entries, list) else []:
        n = entry.get("n") if isinstance(entry, dict) else None
        score = entry.get("score") if isinstance(entry, dict) else None
        if isinstance(n, int) and 1 <= n <= len(rows) and isinstance(score, int):
            store.save_title_score(conn, rows[n - 1]["id"], max(0, min(10, score)))
            scored += 1
    conn.commit()
    run.counts["titles_scored"] += scored


def enrich(conn: store.Connection, settings: Settings, run: Run) -> None:
    """Fetch article text or a transcript for up to MAX_ITEMS_PER_RUN new items in the whole run,
    shared between newsletters; a failed fetch makes the item enrich_failed.
    """
    used: list[int] = []
    for newsletter in settings.newsletters:
        limit = shares(settings.max_items_per_run, len(settings.newsletters), used)
        keys = [s.key for s in newsletter.sources]
        rows = store.items_to_enrich(conn, newsletter.key, keys, limit) if limit else []
        used.append(len(rows))
        for row in rows:
            if run.past_deadline():
                run.degraded = True
                return
            _enrich_item(conn, settings, run, row)


def _enrich_item(conn: store.Connection, settings: Settings, run: Run, row: store.Row) -> None:
    video_id = (
        row["external_id"] if row["source_type"] in YOUTUBE_TYPES else youtube_video_id(row["url"])
    )
    kind: str | None = "transcript" if video_id else "article"
    try:
        if video_id and not settings.transcripts_enabled:
            content = None
            kind = None
        elif video_id:
            content = enrich_mod.transcript(video_id)
        else:
            content = enrich_mod.article(row["url"])
    except Exception as e:
        store.save_content(
            conn,
            row["id"],
            kind=kind,
            text=None,
            error=f"{type(e).__name__}: {e}"[:1000],
            title=None,
        )
        run.counts["enrich_failed"] += 1
    else:
        store.save_content(
            conn,
            row["id"],
            kind=kind,
            text=content.text if content else None,
            error=None,
            title=content.title if content else None,
        )
        run.counts["enriched"] += 1
    conn.commit()


def assess(conn: store.Connection, settings: Settings, run: Run) -> None:
    """Score up to MAX_ITEMS_PER_RUN items in the whole run, shared between newsletters, each
    on its whole stored text against its own newsletter's profile and recent ratings, in batches
    of at most SCORE_BATCH_SIZE items and SCORE_BATCH_CHARS characters. ClaudeUnavailable or
    ClaudeAuthError stops Claude for the rest of the run.
    """
    size = settings.score_batch_size
    used: list[int] = []
    for newsletter in settings.newsletters:
        limit = shares(settings.max_items_per_run, len(settings.newsletters), used)
        keys = [s.key for s in newsletter.sources]
        rows = store.items_to_assess(conn, newsletter.key, keys, limit) if limit else []
        used.append(len(rows))
        ratings = store.recent_ratings(
            conn, newsletter.key, RATINGS_PROMPT_DAYS, RATINGS_PER_SOURCE, RATINGS_PER_SIDE
        )
        conn.commit()
        feedback = _feedback_block(ratings)
        feedback_ids = [r["id"] for r in ratings]
        chance = secrets.SystemRandom()
        for batch in _batches(rows, size, SCORE_BATCH_CHARS):
            if run.claude_stopped:
                return
            if run.past_deadline():
                run.degraded = True
                return
            try:
                _score_group(
                    conn,
                    settings,
                    newsletter,
                    (feedback, feedback_ids),
                    run,
                    batch,
                    retried=False,
                )
                if ratings and chance.random() < RESCORE_SHARE:
                    _rescore_without_ratings(conn, settings, newsletter, run, batch)
            except (llm.ClaudeUnavailable, llm.ClaudeAuthError) as e:
                conn.rollback()
                run.claude_failed(e)


def _batches(rows: list[store.Row], size: int, chars: int) -> list[list[store.Row]]:
    """The rows in order, grouped into batches of at most size rows and chars characters of
    text; a row longer than chars gets a batch of its own.
    """
    batches: list[list[store.Row]] = []
    total = 0
    for row in rows:
        length = len(row.get("text") or "")
        if batches and len(batches[-1]) < size and total + length <= chars:
            batches[-1].append(row)
            total += length
        else:
            batches.append([row])
            total = length
    return batches


def _title_line(title: str, source_key: str | None = None) -> str:
    prefix = f"- {source_key}: " if source_key else "- "
    return prefix + fence(" ".join(title.split())[:RATED_TITLE_LENGTH])


def _feedback_block(ratings: list[store.Row]) -> str:
    def titles(liked: bool) -> list[str]:
        found = [_title_line(r["title"], r["source_key"]) for r in ratings if r["liked"] is liked]
        return found or ["(none yet)"]

    return "\n".join(["Liked:", *titles(True), "Disliked:", *titles(False)])


def _score_group(
    conn: store.Connection,
    settings: Settings,
    newsletter: NewsletterConfig,
    feedback: tuple[str, list[int]],
    run: Run,
    rows: list[store.Row],
    *,
    retried: bool,
) -> None:
    if run.past_deadline():
        run.degraded = True
        return
    prompt = prompts.render(
        SCORE_PROMPT_VERSION,
        interests=newsletter.interests,
        feedback=feedback[0],
        items="\n\n".join(_item_block(n, row) for n, row in enumerate(rows, 1)),
    )
    scored: dict[int, tuple[int, str]] = {}
    try:
        result = llm.structured(prompt, SCORE_SCHEMA, settings.score_model)
        run.record_usage(result)
        scored = _valid_scores(result.output, len(rows))
        run.output_errors = 0
    except llm.ClaudeOutputError as e:
        run.record_usage(*e.spent)
        run.output_errors += 1
        log.warning(
            "invalid scoring output",
            extra={"fields": {"newsletter": newsletter.key, "error": str(e), "size": len(rows)}},
        )
        if run.output_errors >= output_error_limit(settings.score_batch_size):
            raise llm.ClaudeUnavailable(
                f"Claude returned invalid scoring output {run.output_errors} times in a row"
            ) from None
    for n, (score, reason) in scored.items():
        row = rows[n - 1]
        store.save_assessment(
            conn,
            row["id"],
            run_id=run.id,
            model=settings.score_model,
            prompt_version=SCORE_PROMPT_VERSION,
            profile_hash=newsletter.profile_hash,
            score=score,
            reason=reason,
            feedback_ids=feedback[1],
        )
        run.counts["assessed"] += 1
    conn.commit()
    missing = [row for n, row in enumerate(rows, 1) if n not in scored]
    if not missing:
        return
    run.degraded = True
    if not retried:
        _score_group(conn, settings, newsletter, feedback, run, missing, retried=True)
    elif len(missing) == 1:
        if store.record_assess_failure(conn, missing[0]["id"], MAX_ASSESS_ATTEMPTS):
            run.counts["assess_failed"] += 1
        conn.commit()
    else:
        half = len(missing) // 2
        _score_group(conn, settings, newsletter, feedback, run, missing[:half], retried=True)
        _score_group(conn, settings, newsletter, feedback, run, missing[half:], retried=True)


def _rescore_without_ratings(
    conn: store.Connection,
    settings: Settings,
    newsletter: NewsletterConfig,
    run: Run,
    rows: list[store.Row],
) -> None:
    """Score a batch again with no ratings in the prompt, and keep that score beside the first,
    so the two can be compared to see what ratings move. Invalid output is skipped.
    """
    if run.past_deadline():
        return
    prompt = prompts.render(
        SCORE_PROMPT_VERSION,
        interests=newsletter.interests,
        feedback=NO_RATINGS,
        items="\n\n".join(_item_block(n, row) for n, row in enumerate(rows, 1)),
    )
    try:
        result = llm.structured(prompt, SCORE_SCHEMA, settings.score_model)
    except llm.ClaudeOutputError as e:
        run.record_usage(*e.spent)
        log.warning("invalid rescoring output", extra={"fields": {"error": str(e)}})
        return
    run.record_usage(result)
    scores = _valid_scores(result.output, len(rows))
    for n, (score, _reason) in scores.items():
        store.save_score_without_ratings(
            conn,
            rows[n - 1]["id"],
            model=settings.score_model,
            prompt_version=SCORE_PROMPT_VERSION,
            score=score,
        )
    conn.commit()
    run.counts["rescored_without_ratings"] += len(scores)


def _valid_scores(output: Any, size: int) -> dict[int, tuple[int, str]]:
    scored: dict[int, tuple[int, str]] = {}
    entries = output.get("items") if isinstance(output, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        n, score, reason = entry.get("n"), entry.get("score"), entry.get("reason")
        if (
            isinstance(n, int)
            and 1 <= n <= size
            and n not in scored
            and isinstance(score, int)
            and 0 <= score <= 10
            and isinstance(reason, str)
            and reason.strip()
        ):
            scored[n] = (score, reason.strip()[:300])
    return scored


def _item_block(n: int, row: store.Row) -> str:
    return f'<item n="{n}">\n{_item_text(row)}\n</item>'


def _item_text(row: store.Row) -> str:
    lines = [f"Title: {row['title'] or '(no title)'}", f"Source: {row['source_key']}"]
    if row["published_at"]:
        lines.append(f"Published: {row['published_at']:%Y-%m-%d}")
    if row.get("summary"):
        lines.append(f"Summary: {row['summary']}")
    if row.get("text"):
        lines.append(f"Text: {row['text']}")
    if row.get("reason"):
        lines.append(f"Why it was chosen: {row['reason']}")
    return fence("\n".join(lines))


def digest(conn: store.Connection, settings: Settings, run: Run) -> None:
    """Create one digest per newsletter from its best items not yet offered, or nothing for a
    newsletter where none reaches its score threshold. Claude chooses the set, then summarizes
    each included item from its whole text and checks the summary; where it can't, the item
    shows its score reason. A digest addressed to SMTP_USERNAME also lists the items scored below
    the threshold in the last BELOW_THRESHOLD_DAYS days, each once.
    """
    for newsletter in settings.newsletters:
        _digest_newsletter(conn, settings, newsletter, run)


def _digest_newsletter(
    conn: store.Connection,
    settings: Settings,
    newsletter: NewsletterConfig,
    run: Run,
) -> None:
    threshold = newsletter.score_threshold
    keys = [s.key for s in newsletter.sources]
    candidates = store.digest_candidates(conn, newsletter.key, keys, threshold)
    below = (
        store.below_threshold(conn, newsletter.key, keys, threshold, BELOW_THRESHOLD_DAYS)
        if settings.is_owner(newsletter)
        else []
    )
    conn.commit()
    if not candidates:
        log.info("nothing new for a digest", extra={"fields": {"newsletter": newsletter.key}})
        return
    pool, left_out = _offer(candidates, DIGEST_POOL)
    choice = None
    if not run.claude_stopped and not run.past_deadline():
        choice = _choose(conn, settings, newsletter, run, pool)
    fallback = choice is None
    if choice is None:
        run.degraded = True
        choice = _Choice(chosen=pool[: settings.digest_max_items], repeats=set())
    entries = _entries(pool, choice, fallback)
    entries += [(item_id, -1, False, reason) for item_id, reason in left_out]
    entries += [(row["id"], n, False, store.BELOW_THRESHOLD) for n, row in enumerate(below)]
    items = _summaries(conn, settings, newsletter, run, list(choice.chosen))
    store.create_digest(
        conn,
        run.id,
        newsletter.key,
        body={"items": items},
        model=None if fallback else settings.digest_model,
        prompt_version=DIGEST_PROMPT_VERSION,
        fallback=fallback,
        threshold=threshold,
        entries=entries,
    )
    conn.commit()
    run.counts[f"digest_items_{newsletter.key}"] = len(items)
    run.counts[f"below_threshold_{newsletter.key}"] = len(below)


def _offer(
    candidates: list[store.Row], max_items: int
) -> tuple[list[store.Row], list[tuple[int, str]]]:
    """The candidates for the digest call, best first: at most DIGEST_SOURCE_CAP from one source
    and max_items in all. The rest are left out, each with its reason, "source_cap" or "cap", and
    recorded as offered, so they don't come back the next day.
    """
    per_source: Counter[str] = Counter()
    offered: list[store.Row] = []
    left_out: list[tuple[int, str]] = []
    for row in candidates:
        if per_source[row["source_key"]] >= DIGEST_SOURCE_CAP:
            left_out.append((row["id"], "source_cap"))
        elif len(offered) >= max_items:
            left_out.append((row["id"], "cap"))
        else:
            per_source[row["source_key"]] += 1
            offered.append(row)
    return offered, left_out


def _entries(
    pool: list[store.Row], choice: _Choice, fallback: bool
) -> list[tuple[int, int, bool, str | None]]:
    """The digest_items rows for the pool: the chosen in reading order, then the rest left out
    as "repeat", "not_chosen", or "cap" when Claude didn't choose.
    """
    chosen = {row["id"] for row in choice.chosen}
    entries: list[tuple[int, int, bool, str | None]] = [
        (row["id"], position, True, None) for position, row in enumerate(choice.chosen)
    ]
    unchosen = "cap" if fallback else "not_chosen"
    for row in pool:
        if row["id"] not in chosen:
            reason = "repeat" if row["id"] in choice.repeats else unchosen
            entries.append((row["id"], -1, False, reason))
    return entries


def _choose(
    conn: store.Connection,
    settings: Settings,
    newsletter: NewsletterConfig,
    run: Run,
    pool: list[store.Row],
) -> _Choice | None:
    """Ask Claude to choose the digest's set from the pool, shown in random order without
    scores, beside the owner's recent ratings and the titles sent in the last SENT_TITLE_DAYS
    days. None when Claude can't, so the caller falls back to score order.
    """
    ratings = store.recent_ratings(
        conn, newsletter.key, RATINGS_PROMPT_DAYS, RATINGS_PER_SOURCE, RATINGS_PER_SIDE
    )
    sent = store.sent_titles(conn, newsletter.key, SENT_TITLE_DAYS)
    conn.commit()
    shown = list(pool)
    secrets.SystemRandom().shuffle(shown)
    prompt = prompts.render(
        DIGEST_PROMPT_VERSION,
        name=newsletter.name,
        interests=newsletter.interests,
        feedback=_feedback_block(ratings),
        sent="\n".join(_title_line(title) for title in sent) or "(none)",
        max_items=str(settings.digest_max_items),
        items="\n\n".join(_item_block(n, row) for n, row in enumerate(shown, 1)),
    )
    try:
        result = llm.structured(prompt, DIGEST_SCHEMA, settings.digest_model)
    except llm.ClaudeOutputError as e:
        run.record_usage(*e.spent)
        log.warning(
            "invalid digest output",
            extra={"fields": {"newsletter": newsletter.key, "error": str(e)}},
        )
        return None
    except (llm.ClaudeUnavailable, llm.ClaudeAuthError) as e:
        run.claude_failed(e)
        return None
    run.record_usage(result)
    return _valid_choice(result.output, shown, settings.digest_max_items)


def _valid_choice(output: Any, shown: list[store.Row], max_items: int) -> _Choice | None:
    output = output if isinstance(output, dict) else {}
    chosen: list[store.Row] = []
    seen: set[int] = set()
    entries = output.get("items")
    for entry in entries if isinstance(entries, list) else []:
        n = entry.get("n") if isinstance(entry, dict) else None
        if (
            len(chosen) < max_items
            and isinstance(n, int)
            and 1 <= n <= len(shown)
            and n not in seen
        ):
            seen.add(n)
            chosen.append(shown[n - 1])
    if not chosen:
        return None
    named = output.get("repeats")
    repeats = {
        shown[n - 1]["id"]
        for n in (named if isinstance(named, list) else [])
        if isinstance(n, int) and 1 <= n <= len(shown) and n not in seen
    }
    return _Choice(chosen=chosen, repeats=repeats)


def _summaries(
    conn: store.Connection,
    settings: Settings,
    newsletter: NewsletterConfig,
    run: Run,
    rows: list[store.Row],
) -> list[dict[str, Any]]:
    """The digest body's items in reading order. Each item Claude could summarize from its whole
    text carries the summary and what the check found; the others carry only their ID, and the
    email shows their score reason.
    """
    texts = store.item_texts(conn, [row["id"] for row in rows])
    conn.commit()
    items: list[dict[str, Any]] = []
    for row in rows:
        source = texts.get(row["id"])
        done = None
        if source is not None and source["text"] and not run.claude_stopped:
            if run.past_deadline():
                run.degraded = True
            else:
                done = _summarize(settings, newsletter, run, source)
        if done is None:
            run.counts["unsummarized"] += 1
            items.append({"item_id": row["id"]})
        else:
            items.append({"item_id": row["id"], "summary": done[0], "check": done[1]})
    return items


def _summarize(
    settings: Settings, newsletter: NewsletterConfig, run: Run, source: store.Row
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Claude's summary of one item from its whole text, checked against the text and rewritten
    once when the check finds unsupported statements or missing points, with what the check
    found. None when Claude can't write one.
    """
    item = _item_text(source)
    try:
        first = _summary_call(SUMMARY_PROMPT_VERSION, settings, newsletter, run, item=item)
        if first is None:
            return None
        run.counts["summaries"] += 1
        found = _check_summary(settings, run, item, first)
        if found is None:
            return first, {"checked": False}
        unsupported, missing = found
        if not (unsupported or missing):
            return first, {"checked": True, "unsupported": 0, "missing": 0, "revised": False}
        revised = _summary_call(
            REVISE_PROMPT_VERSION,
            settings,
            newsletter,
            run,
            item=item,
            summary=fence(_summary_text(first)),
            check=fence(_check_text(unsupported, missing)),
        )
    except (llm.ClaudeUnavailable, llm.ClaudeAuthError) as e:
        run.claude_failed(e)
        return None
    if revised is not None:
        run.counts["summaries_revised"] += 1
    report = {
        "checked": True,
        "unsupported": len(unsupported),
        "missing": len(missing),
        "revised": revised is not None,
    }
    return revised or first, report


def _summary_call(
    version: str, settings: Settings, newsletter: NewsletterConfig, run: Run, **values: str
) -> dict[str, Any] | None:
    prompt = prompts.render(version, name=newsletter.name, interests=newsletter.interests, **values)
    try:
        result = llm.structured(prompt, SUMMARY_SCHEMA, settings.digest_model)
    except llm.ClaudeOutputError as e:
        run.record_usage(*e.spent)
        log.warning(
            "invalid summary output", extra={"fields": {"prompt": version, "error": str(e)}}
        )
        return None
    run.record_usage(result)
    return _valid_summary(result.output)


def _valid_summary(output: Any) -> dict[str, Any] | None:
    output = output if isinstance(output, dict) else {}
    gist, takeaways, highlights = (
        output.get("gist"),
        output.get("takeaways"),
        output.get("highlights"),
    )
    points = (
        [t.strip() for t in takeaways if isinstance(t, str) and t.strip()]
        if isinstance(takeaways, list)
        else []
    )
    if not (isinstance(gist, str) and gist.strip() and points):
        return None
    return {
        "gist": gist.strip(),
        "takeaways": points[:MAX_TAKEAWAYS],
        "highlights": highlights.strip() if isinstance(highlights, str) else "",
    }


def _check_summary(
    settings: Settings, run: Run, item: str, summary: dict[str, Any]
) -> tuple[list[tuple[str, str]], list[str]] | None:
    """What a second call finds wrong with a summary: the statements the source doesn't support,
    each with why, and the important points it leaves out. None when Claude can't check it.
    """
    prompt = prompts.render(CHECK_PROMPT_VERSION, item=item, summary=fence(_summary_text(summary)))
    try:
        result = llm.structured(prompt, CHECK_SCHEMA, settings.digest_model)
    except llm.ClaudeOutputError as e:
        run.record_usage(*e.spent)
        log.warning("invalid check output", extra={"fields": {"error": str(e)}})
        return None
    run.record_usage(result)
    output = result.output if isinstance(result.output, dict) else {}
    found = output.get("unsupported")
    unsupported = [
        (u["claim"].strip(), u["why"].strip())
        for u in (found if isinstance(found, list) else [])
        if isinstance(u, dict) and isinstance(u.get("claim"), str) and isinstance(u.get("why"), str)
    ]
    left = output.get("missing")
    missing = [m.strip() for m in (left if isinstance(left, list) else []) if isinstance(m, str)]
    return unsupported, [m for m in missing if m][:MAX_MISSING]


def _summary_text(summary: dict[str, Any]) -> str:
    lines = [summary["gist"], *(f"- {t}" for t in summary["takeaways"])]
    if summary["highlights"]:
        lines.append(f"Highlights: {summary['highlights']}")
    return "\n".join(lines)


def _check_text(unsupported: list[tuple[str, str]], missing: list[str]) -> str:
    lines = ["Unsupported:", *(f"- {claim} ({why})" for claim, why in unsupported)]
    lines += ["Missing:", *(f"- {point}" for point in missing)]
    return "\n".join(lines)


def deliver(conn: store.Connection, settings: Settings, run: Run | None) -> int:
    """Email every unsent digest to its newsletter's recipient; a failed send leaves it unsent for
    the next try. Only digests addressed to SMTP_USERNAME carry the health footer, the rating
    links and the list of items below the threshold. Returns how many were sent.
    """
    sent = 0
    newsletters = {n.key: n for n in settings.newsletters}
    for row in store.unsent_digests(conn, list(newsletters)):
        newsletter = newsletters[row["newsletter"]]
        where = {"digest": row["id"], "newsletter": row["newsletter"]}
        owner = settings.is_owner(newsletter)
        entries = store.digest_entries(conn, row["id"])
        listed = store.digest_listed(conn, row["id"]) if owner else []
        summaries = {e["item_id"]: e.get("summary") for e in row["body_json"].get("items", [])}
        health = (
            {
                **store.health(conn, newsletter.key, [s.key for s in newsletter.sources]),
                "ratings_error": run.ratings_error if run is not None else None,
            }
            if owner
            else None
        )
        email = mail.render_digest(
            name=newsletter.name,
            entries=entries,
            listed=listed,
            summaries=summaries,
            source_names={s.key: s.label for s in newsletter.sources},
            fallback=row["fallback"],
            health=health,
            feedback_to=settings.mail.smtp_username if owner else None,
            token_days_left=settings.token_days_left(date.today()),
            today=date.today(),
        )
        conn.commit()
        try:
            mail.send(email, settings.mail, newsletter.recipient)
        except Exception as e:
            error = mail.error_summary(e)
            store.mark_send_error(conn, row["id"], error)
            conn.commit()
            if run is not None:
                run.counts["send_failures"] += 1
                run.degraded = True
            log.warning("digest not sent", extra={"fields": {**where, "error": error}})
            continue
        store.mark_sent(conn, row["id"])
        conn.commit()
        sent += 1
        log.info(
            "digest sent",
            extra={"fields": {**where, "items": len(entries), "to_owner": owner}},
        )
    return sent


def suggest_profile(conn: store.Connection, settings: Settings, run: Run) -> None:
    """Email the owner Claude's suggested edits to the interest profile of each newsletter
    addressed to SMTP_USERNAME, at most once every PROFILE_EVERY and only once PROFILE_MIN_RATINGS
    ratings arrived since the last suggestions. The agent never edits the profile itself; a
    failure is logged and the run goes on as partial.
    """
    for newsletter in settings.newsletters:
        if run.claude_stopped or run.past_deadline():
            return
        if not settings.is_owner(newsletter):
            continue
        last = store.last_profile_suggestion(conn, newsletter.key)
        due = last is None or last <= datetime.now(UTC) - PROFILE_EVERY
        ratings = (
            store.ratings_since(conn, newsletter.key, last, PROFILE_MAX_RATINGS) if due else []
        )
        conn.commit()
        if len(ratings) >= PROFILE_MIN_RATINGS:
            _suggest_profile(conn, settings, newsletter, run, ratings)


def _suggest_profile(
    conn: store.Connection,
    settings: Settings,
    newsletter: NewsletterConfig,
    run: Run,
    ratings: list[store.Row],
) -> None:
    lines = [
        f"{'more' if r['liked'] else 'less'} {_title_line(r['title'], r['source_key'])[2:]}"
        for r in ratings
    ]
    prompt = prompts.render(
        PROFILE_PROMPT_VERSION,
        name=newsletter.name,
        interests=newsletter.interests,
        ratings="\n".join(f"- {line}" for line in lines),
    )
    try:
        result = llm.structured(prompt, PROFILE_SCHEMA, settings.digest_model)
    except llm.ClaudeOutputError as e:
        run.record_usage(*e.spent)
        run.degraded = True
        log.warning("invalid profile suggestions", extra={"fields": {"error": str(e)}})
        return
    except (llm.ClaudeUnavailable, llm.ClaudeAuthError) as e:
        run.claude_failed(e)
        return
    run.record_usage(result)
    entries = result.output.get("suggestions") if isinstance(result.output, dict) else None
    suggestions = [
        {"change": e["change"].strip(), "why": e["why"].strip()}
        for e in (entries if isinstance(entries, list) else [])[:5]
        if isinstance(e, dict)
        and isinstance(e.get("change"), str)
        and isinstance(e.get("why"), str)
        and e["change"].strip()
    ]
    email = mail.render_profile_suggestions(
        newsletter.name, suggestions, len(ratings), date.today()
    )
    try:
        mail.send(email, settings.mail, settings.mail.smtp_username)
    except Exception as e:
        run.degraded = True
        log.warning(
            "profile suggestions not sent", extra={"fields": {"error": mail.error_summary(e)}}
        )
        return
    store.save_profile_suggestion(conn, newsletter.key, run.id, {"suggestions": suggestions})
    conn.commit()
    run.counts["profile_suggestions"] += 1
