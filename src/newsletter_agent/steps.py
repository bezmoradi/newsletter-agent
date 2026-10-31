"""The steps of a run: read ratings, collect, enrich, assess, digest and deliver."""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from newsletter_agent import enrich as enrich_mod
from newsletter_agent import llm, mail, prompts, store
from newsletter_agent.settings import NewsletterConfig, Settings, SourceConfig
from newsletter_agent.sources import COLLECTORS
from newsletter_agent.sources.base import Collected, DeadlinePassed, SourceState
from newsletter_agent.text import fence
from newsletter_agent.urls import MAX_URL_LENGTH, canonical_url, is_web_url, youtube_video_id

log = logging.getLogger(__name__)

SCORE_PROMPT_VERSION = "score_v2"
DIGEST_PROMPT_VERSION = "digest_v1"
RATINGS_READ_DAYS = 30
RATINGS_PROMPT_DAYS = 60
RATINGS_PER_SIDE = 5
RATED_TITLE_LENGTH = 200
SCORE_EXCERPT = 4000
MAX_ASSESS_ATTEMPTS = 3
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
        "intro": {"type": "string", "maxLength": 800},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "summary": {"type": "string", "maxLength": 600},
                },
                "required": ["n", "summary"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["intro", "items"],
    "additionalProperties": False,
}


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
    against its own newsletter's profile and recent ratings, in batches. ClaudeUnavailable or
    ClaudeAuthError stops Claude for the rest of the run.
    """
    size = settings.score_batch_size
    used: list[int] = []
    for newsletter in settings.newsletters:
        limit = shares(settings.max_items_per_run, len(settings.newsletters), used)
        keys = [s.key for s in newsletter.sources]
        rows = (
            store.items_to_assess(conn, newsletter.key, keys, limit, SCORE_EXCERPT) if limit else []
        )
        used.append(len(rows))
        ratings = store.recent_ratings(conn, newsletter.key, RATINGS_PROMPT_DAYS, RATINGS_PER_SIDE)
        conn.commit()
        feedback = _feedback_block(ratings)
        for start in range(0, len(rows), size):
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
                    feedback,
                    run,
                    rows[start : start + size],
                    retried=False,
                )
            except (llm.ClaudeUnavailable, llm.ClaudeAuthError) as e:
                conn.rollback()
                run.claude_failed(e)


def _feedback_block(ratings: list[store.Row]) -> str:
    def titles(liked: bool) -> list[str]:
        found = [
            "- " + fence(" ".join(r["title"].split())[:RATED_TITLE_LENGTH])
            for r in ratings
            if r["liked"] is liked
        ]
        return found or ["(none yet)"]

    return "\n".join(["Liked:", *titles(True), "Disliked:", *titles(False)])


def _score_group(
    conn: store.Connection,
    settings: Settings,
    newsletter: NewsletterConfig,
    feedback: str,
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
        feedback=feedback,
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
    lines = [f"Title: {row['title'] or '(no title)'}", f"Source: {row['source_key']}"]
    if row["published_at"]:
        lines.append(f"Published: {row['published_at']:%Y-%m-%d}")
    if row.get("summary"):
        lines.append(f"Summary: {row['summary']}")
    if row.get("excerpt"):
        lines.append(f"Text: {row['excerpt']}")
    if row.get("reason"):
        lines.append(f"Why it was chosen: {row['reason']}")
    return f'<item n="{n}">\n{fence(chr(10).join(lines))}\n</item>'


def digest(conn: store.Connection, settings: Settings, run: Run) -> None:
    """Create one digest per newsletter from its best items not yet offered, or nothing for a
    newsletter where none reaches its score threshold. Falls back to the score reasons when
    Claude can't write it.
    """
    for newsletter in settings.newsletters:
        _digest_newsletter(conn, settings, newsletter, run)


def _digest_newsletter(
    conn: store.Connection,
    settings: Settings,
    newsletter: NewsletterConfig,
    run: Run,
) -> None:
    candidates = store.digest_candidates(conn, newsletter.key, newsletter.score_threshold)
    conn.commit()
    if not candidates:
        log.info("nothing new for a digest", extra={"fields": {"newsletter": newsletter.key}})
        return
    offered = candidates[: settings.digest_max_items]
    body: dict[str, Any] | None = None
    if not run.claude_stopped and not run.past_deadline():
        body = _write_digest(settings, newsletter, run, offered)
    fallback = body is None
    if body is None:
        run.degraded = True
        body = {
            "intro": None,
            "items": [{"item_id": row["id"], "summary": row["reason"]} for row in offered],
        }
    order = {entry["item_id"]: position for position, entry in enumerate(body["items"])}
    entries = [(row["id"], order[row["id"]], True) for row in offered]
    entries += [(row["id"], -1, False) for row in candidates[settings.digest_max_items :]]
    store.create_digest(
        conn,
        run.id,
        newsletter.key,
        body=body,
        model=None if fallback else settings.digest_model,
        prompt_version=DIGEST_PROMPT_VERSION,
        fallback=fallback,
        entries=entries,
    )
    conn.commit()
    run.counts[f"digest_items_{newsletter.key}"] = len(offered)


def _write_digest(
    settings: Settings, newsletter: NewsletterConfig, run: Run, offered: list[store.Row]
) -> dict[str, Any] | None:
    prompt = prompts.render(
        DIGEST_PROMPT_VERSION,
        name=newsletter.name,
        interests=newsletter.interests,
        items="\n\n".join(_item_block(n, row) for n, row in enumerate(offered, 1)),
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
    output = result.output if isinstance(result.output, dict) else {}
    items = []
    seen: set[int] = set()
    entries = output.get("items")
    for entry in entries if isinstance(entries, list) else []:
        n = entry.get("n") if isinstance(entry, dict) else None
        summary = entry.get("summary") if isinstance(entry, dict) else None
        if (
            isinstance(n, int)
            and 1 <= n <= len(offered)
            and n not in seen
            and isinstance(summary, str)
        ):
            seen.add(n)
            items.append({"item_id": offered[n - 1]["id"], "summary": summary.strip()})
    for n, row in enumerate(offered, 1):
        if n not in seen:
            items.append({"item_id": row["id"], "summary": row["reason"]})
    intro = output.get("intro")
    return {"intro": intro.strip() if isinstance(intro, str) else None, "items": items}


def deliver(conn: store.Connection, settings: Settings, run: Run | None) -> int:
    """Email every unsent digest to its newsletter's recipient; a failed send leaves it unsent for
    the next try. Only digests addressed to SMTP_USERNAME carry the health footer and the rating
    links. Returns how many were sent.
    """
    sent = 0
    token = store.feedback_token(conn)
    newsletters = {n.key: n for n in settings.newsletters}
    for row in store.unsent_digests(conn, list(newsletters)):
        newsletter = newsletters[row["newsletter"]]
        where = {"digest": row["id"], "newsletter": row["newsletter"]}
        owner = settings.is_owner(newsletter)
        entries = store.digest_entries(conn, row["id"])
        summaries = {e["item_id"]: e["summary"] for e in row["body_json"].get("items", [])}
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
            intro=row["body_json"].get("intro"),
            entries=entries,
            summaries=summaries,
            source_names={s.key: s.label for s in newsletter.sources},
            fallback=row["fallback"],
            health=health,
            feedback_to=settings.mail.smtp_username if owner else None,
            feedback_token=token,
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
