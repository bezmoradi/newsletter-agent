"""All SQL. The only module that imports psycopg."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from newsletter_agent.mail import Rating
from newsletter_agent.settings import SourceConfig
from newsletter_agent.sources.base import Candidate
from newsletter_agent.text import storable

LOCK_KEY = 7_311_002
BELOW_THRESHOLD = "below_threshold"
Row = dict[str, Any]
Connection = psycopg.Connection[Row]


class MalformedUrl(Exception):
    pass


def _safe(text: str | None) -> str | None:
    """text.storable, keeping None. Error messages and fetched text can carry NUL characters or
    lone surrogates, and Postgres would refuse the whole write.
    """
    return None if text is None else storable(text)


class InsecureConnection(Exception):
    pass


def connect(url: str, *, autocommit: bool = False) -> Connection:
    """Connect with a 10 s connect timeout and a 60 s statement timeout, over TLS that checks the
    server's certificate and host name (sslmode=verify-full, whatever DATABASE_URL says). Raises
    MalformedUrl, without the URL's text, when DATABASE_URL can't be parsed, because psycopg's own
    message would include the password, and InsecureConnection when the connection isn't
    encrypted.
    """
    try:
        conn = psycopg.connect(
            url,
            sslmode="verify-full",
            connect_timeout=10,
            options="-c statement_timeout=60000",
            autocommit=autocommit,
            row_factory=dict_row,
        )
    except psycopg.ProgrammingError:
        raise MalformedUrl("DATABASE_URL is malformed; check it in agent.env") from None
    if not conn.pgconn.ssl_in_use:
        conn.close()
        raise InsecureConnection(
            "the database connection doesn't use TLS; DATABASE_URL needs sslmode=verify-full"
        )
    return conn


def rollback_quietly(conn: Connection) -> None:
    """Roll back, ignoring a connection that is already lost."""
    with contextlib.suppress(psycopg.Error):
        conn.rollback()


@contextlib.contextmanager
def locked(url: str) -> Iterator[tuple[Connection, bool]]:
    """Two connections: one holds the run lock for its session, the other does the work. Yields
    the working connection and whether the lock was taken; both close on exit.
    """
    with connect(url, autocommit=True) as lock_conn, connect(url) as conn:
        yield conn, try_lock(lock_conn)


def try_lock(conn: Connection) -> bool:
    """Take the run lock for this connection's session; False when another run holds it."""
    row = conn.execute("select pg_try_advisory_lock(%s) as locked", (LOCK_KEY,)).fetchone()
    return bool(row and row["locked"])


def _migrations() -> list[tuple[str, str]]:
    folder = resources.files("newsletter_agent") / "migrations"
    files = sorted((f for f in folder.iterdir() if f.name.endswith(".sql")), key=lambda f: f.name)
    return [(f.name.removesuffix(".sql"), f.read_text(encoding="utf-8")) for f in files]


def pending_migrations(conn: Connection) -> list[str]:
    exists = conn.execute("select to_regclass('schema_migrations') as t").fetchone()
    applied: set[str] = set()
    if exists and exists["t"]:
        applied = {r["version"] for r in conn.execute("select version from schema_migrations")}
    conn.rollback()
    return [version for version, _ in _migrations() if version not in applied]


def migrate(conn: Connection) -> list[str]:
    """Apply pending migrations, each in its own transaction, and return the versions applied."""
    conn.execute(
        "create table if not exists schema_migrations "
        "(version text primary key, applied_at timestamptz not null default now())"
    )
    conn.commit()
    applied = {r["version"] for r in conn.execute("select version from schema_migrations")}
    done = []
    for version, sql in _migrations():
        if version in applied:
            continue
        conn.execute(sql.encode())
        conn.execute("insert into schema_migrations (version) values (%s)", (version,))
        conn.commit()
        done.append(version)
    conn.commit()
    return done


def unfinished_run_started(conn: Connection) -> datetime | None:
    """When the oldest run that hasn't finished started, or None."""
    try:
        row = conn.execute(
            "select min(started_at) as started from runs where finished_at is null"
        ).fetchone()
    except psycopg.errors.UndefinedTable:
        row = None
    conn.rollback()
    return row["started"] if row else None


def abandon_unfinished_runs(conn: Connection) -> int:
    cur = conn.execute(
        "update runs set status = 'abandoned', finished_at = now() where finished_at is null"
    )
    conn.commit()
    return cur.rowcount


def open_run(conn: Connection) -> int:
    row = conn.execute("insert into runs (status) values ('running') returning id").fetchone()
    conn.commit()
    assert row is not None
    return int(row["id"])


def close_run(
    conn: Connection,
    run_id: int,
    *,
    status: str,
    counts: dict[str, int],
    usage: dict[str, Any],
    error: str | None,
    claude_unavailable: bool,
    notice_sent: bool,
) -> None:
    conn.execute(
        "update runs set finished_at = now(), status = %s, counts_json = %s, usage_json = %s, "
        "error = %s, claude_unavailable = %s, notice_sent = %s where id = %s",
        (
            status,
            Jsonb(counts),
            Jsonb(usage),
            _safe(error),
            claude_unavailable,
            notice_sent,
            run_id,
        ),
    )
    conn.commit()


def unavailable_streak(conn: Connection, before_run: int, limit: int) -> int:
    """How many of the runs just before before_run, up to limit, in a row had Claude unavailable."""
    rows = conn.execute(
        "select claude_unavailable from runs where id < %s and status <> 'abandoned' "
        "order by id desc limit %s",
        (before_run, limit),
    ).fetchall()
    streak = 0
    for row in rows:
        if not row["claude_unavailable"]:
            break
        streak += 1
    return streak


def all_sources_failed_streak(conn: Connection, before_run: int, limit: int) -> int:
    """How many of the runs just before before_run, up to limit, in a row tried at least one
    source and had every one fail.
    """
    rows = conn.execute(
        "select counts_json from runs where id < %s and status <> 'abandoned' "
        "order by id desc limit %s",
        (before_run, limit),
    ).fetchall()
    streak = 0
    for row in rows:
        counts = row["counts_json"] or {}
        tried = counts.get("sources_tried", 0)
        if not tried or counts.get("source_failures", 0) < tried:
            break
        streak += 1
    return streak


def sync_source(conn: Connection, newsletter: str, source: SourceConfig) -> Row:
    """Upsert the source's row. When its definition (type, URL, channel or include rules) changed
    since it was last stored, clear its first sight, ETag and Last-Modified, so the next
    collection takes a new baseline instead of treating every old entry as new.
    """
    url = source.url or source.channel_id
    row = conn.execute(
        "insert into sources (newsletter, key, type, url, definition) values (%s, %s, %s, %s, %s) "
        "on conflict (newsletter, key) do update set type = excluded.type, url = excluded.url, "
        "first_seen_at = case when sources.definition is not null "
        "and sources.definition <> excluded.definition then null else sources.first_seen_at end, "
        "etag = case when sources.definition is not null "
        "and sources.definition <> excluded.definition then null else sources.etag end, "
        "last_modified = case when sources.definition is not null "
        "and sources.definition <> excluded.definition then null else sources.last_modified end, "
        "definition = excluded.definition returning *",
        (newsletter, source.key, source.type, url, source.definition),
    ).fetchone()
    assert row is not None
    return row


def insert_item(
    conn: Connection,
    newsletter: str,
    source_id: int,
    item: Candidate,
    canonical: str,
    state: str,
) -> bool:
    """Insert unless the source's ID, or the canonical URL within this newsletter, is already
    stored; True when a row was added.
    """
    row = conn.execute(
        "insert into items (newsletter, source_id, external_id, url, canonical_url, title, "
        "published_at, summary, state, search_terms) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict do nothing returning id",
        (
            newsletter,
            source_id,
            _safe(item.external_id),
            _safe(item.url),
            _safe(canonical),
            _safe(item.title),
            item.published_at,
            _safe(item.summary),
            state,
            [_safe(term) for term in item.search_terms] or None,
        ),
    ).fetchone()
    return row is not None


def record_source_success(
    conn: Connection,
    source_id: int,
    *,
    now: datetime,
    etag: str | None,
    last_modified: str | None,
    new_items: int,
) -> None:
    conn.execute(
        "update sources set etag = %s, last_modified = %s, "
        "first_seen_at = coalesce(first_seen_at, %s), last_checked_at = %s, "
        "last_success_at = %s, consecutive_failures = 0, last_error = null, "
        "last_new_item_at = case when %s > 0 then %s else last_new_item_at end where id = %s",
        (etag, last_modified, now, now, now, new_items, now, source_id),
    )


def record_source_failure(conn: Connection, source_id: int, *, now: datetime, error: str) -> None:
    conn.execute(
        "update sources set last_checked_at = %s, consecutive_failures = consecutive_failures + 1, "
        "last_error = %s where id = %s",
        (now, _safe(error[:1000]), source_id),
    )


def items_to_enrich(
    conn: Connection, newsletter: str, source_keys: list[str], limit: int
) -> list[Row]:
    """Up to limit new items of the sources in source_keys, taken one source at a time: each
    source's best item first, then each source's second best, and so on, so a busy source can't
    crowd out the others. Within a source, items go by their title score, then newest first.
    """
    return conn.execute(
        "select id, external_id, url, title, source_type from (select i.id, i.external_id, "
        "i.url, i.title, s.type as source_type, "
        "coalesce(i.published_at, i.first_seen_at) as sorted_at, row_number() over "
        "(partition by i.source_id order by i.title_score desc nulls last, "
        "coalesce(i.published_at, i.first_seen_at) desc, i.id desc) as source_rank "
        "from items i join sources s on s.id = i.source_id "
        "where i.newsletter = %s and s.key = any(%s) and i.state = 'new') ranked "
        "order by source_rank, sorted_at desc, id desc limit %s",
        (newsletter, source_keys, limit),
    ).fetchall()


def new_item_count(conn: Connection, newsletter: str, source_keys: list[str]) -> int:
    row = conn.execute(
        "select count(*) as n from items i join sources s on s.id = i.source_id "
        "where i.newsletter = %s and s.key = any(%s) and i.state = 'new'",
        (newsletter, source_keys),
    ).fetchone()
    return int(row["n"]) if row else 0


def untriaged_items(
    conn: Connection, newsletter: str, source_keys: list[str], limit: int
) -> list[Row]:
    """Up to limit new items of the sources in source_keys that have a title but no title
    score, newest first.
    """
    return conn.execute(
        "select i.id, i.title, i.published_at, s.key as source_key from items i "
        "join sources s on s.id = i.source_id where i.newsletter = %s and s.key = any(%s) "
        "and i.state = 'new' and i.title_score is null and i.title is not null "
        "order by coalesce(i.published_at, i.first_seen_at) desc, i.id desc limit %s",
        (newsletter, source_keys, limit),
    ).fetchall()


def save_title_score(conn: Connection, item_id: int, score: int) -> None:
    conn.execute("update items set title_score = %s where id = %s", (score, item_id))


def expire_items(conn: Connection, days: int) -> int:
    """Move items still waiting to be fetched or scored, whose date, or first sight when they have
    none, is more than `days` days old, back to baseline; a backfilled item counts from its
    backfill. Returns how many moved.
    """
    cur = conn.execute(
        "update items set state = 'baseline' where state in ('new', 'enriched', 'enrich_failed') "
        "and coalesce(queued_at, published_at, first_seen_at) < now() - make_interval(days => %s)",
        (days,),
    )
    return cur.rowcount


def save_content(
    conn: Connection,
    item_id: int,
    *,
    kind: str | None,
    text: str | None,
    error: str | None,
    title: str | None,
) -> None:
    if kind is not None:
        conn.execute(
            "insert into item_content (item_id, kind, text, error) values (%s, %s, %s, %s) "
            "on conflict (item_id) do update set kind = excluded.kind, text = excluded.text, "
            "error = excluded.error, fetched_at = now()",
            (item_id, kind, _safe(text), _safe(error)),
        )
    conn.execute(
        "update items set state = %s, title = coalesce(title, %s) where id = %s",
        ("enrich_failed" if error else "enriched", _safe(title), item_id),
    )


def items_to_assess(
    conn: Connection, newsletter: str, source_keys: list[str], limit: int
) -> list[Row]:
    """Up to limit fetched items of the sources in source_keys, taken one source at a time like
    items_to_enrich, each with its whole stored text, so an item is judged on all of it.
    """
    return conn.execute(
        "select id, url, title, summary, published_at, source_key, text from (select i.id, "
        "i.url, i.title, i.summary, i.published_at, s.key as source_key, c.text, "
        "coalesce(i.published_at, i.first_seen_at) as sorted_at, "
        "row_number() over (partition by i.source_id order by i.title_score desc nulls last, "
        "coalesce(i.published_at, i.first_seen_at) desc, i.id desc) as source_rank "
        "from items i join sources s on s.id = i.source_id "
        "left join item_content c on c.item_id = i.id "
        "where i.newsletter = %s and s.key = any(%s) "
        "and i.state in ('enriched', 'enrich_failed')) ranked "
        "order by source_rank, sorted_at desc, id desc limit %s",
        (newsletter, source_keys, limit),
    ).fetchall()


def save_assessment(
    conn: Connection,
    item_id: int,
    *,
    run_id: int,
    model: str,
    prompt_version: str,
    profile_hash: str,
    score: int,
    reason: str,
    feedback_ids: list[int],
) -> None:
    conn.execute(
        "insert into assessments (item_id, model, prompt_version, profile_hash, score, reason, "
        "run_id, feedback_ids) values (%s, %s, %s, %s, %s, %s, %s, %s) on conflict do nothing",
        (item_id, model, prompt_version, profile_hash, score, _safe(reason), run_id, feedback_ids),
    )
    conn.execute("update items set state = 'assessed' where id = %s", (item_id,))


def save_score_without_ratings(
    conn: Connection, item_id: int, *, model: str, prompt_version: str, score: int
) -> None:
    conn.execute(
        "update assessments set score_without_ratings = %s "
        "where item_id = %s and model = %s and prompt_version = %s",
        (score, item_id, model, prompt_version),
    )


def record_assess_failure(conn: Connection, item_id: int, max_attempts: int) -> bool:
    """Count a failed scoring attempt; True when this attempt made the item assess_failed."""
    row = conn.execute(
        "update items set assess_attempts = assess_attempts + 1, "
        "state = case when assess_attempts + 1 >= %s then 'assess_failed' else state end "
        "where id = %s returning state",
        (max_attempts, item_id),
    ).fetchone()
    return bool(row and row["state"] == "assess_failed")


def digest_candidates(
    conn: Connection, newsletter: str, source_keys: list[str], threshold: int
) -> list[Row]:
    """A newsletter's assessed items of the sources in source_keys, at or above threshold, that
    were never offered to a digest, best first.
    """
    return _scored(conn, newsletter, source_keys, threshold, 11)


def below_threshold(
    conn: Connection, newsletter: str, source_keys: list[str], threshold: int, days: int
) -> list[Row]:
    """A newsletter's assessed items of the sources in source_keys whose latest score, given in
    the last days days, is below threshold, never offered to a digest or listed in one, best
    first.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    rows = _scored(conn, newsletter, source_keys, 0, threshold)
    return [row for row in rows if row["scored_at"] >= since]


def _scored(
    conn: Connection, newsletter: str, source_keys: list[str], low: int, high: int
) -> list[Row]:
    return conn.execute(
        "select * from (select distinct on (i.id) i.id, i.url, i.title, i.summary, "
        "i.published_at, s.key as source_key, a.score, a.reason, a.created_at as scored_at, "
        "left(c.text, 1500) as text from items i "
        "join sources s on s.id = i.source_id join assessments a on a.item_id = i.id "
        "left join item_content c on c.item_id = i.id "
        "left join digest_items d on d.item_id = i.id "
        "where i.newsletter = %s and s.key = any(%s) and i.state = 'assessed' "
        "and d.item_id is null "
        "order by i.id, a.created_at desc) latest where score >= %s and score < %s "
        "order by score desc, published_at desc nulls last",
        (newsletter, source_keys, low, high),
    ).fetchall()


def item_texts(conn: Connection, item_ids: list[int]) -> dict[int, Row]:
    """The given items' titles, sources, descriptions and whole stored texts, by item ID."""
    rows = conn.execute(
        "select i.id, i.title, i.summary, i.published_at, s.key as source_key, c.text "
        "from items i join sources s on s.id = i.source_id "
        "left join item_content c on c.item_id = i.id where i.id = any(%s)",
        (item_ids,),
    ).fetchall()
    return {int(r["id"]): r for r in rows}


def sent_titles(conn: Connection, newsletter: str, days: int) -> list[str]:
    """The titles of the items a newsletter's digests sent in the last `days` days."""
    rows = conn.execute(
        "select i.title from digest_items di join digests d on d.id = di.digest_id "
        "join items i on i.id = di.item_id where d.newsletter = %s and di.included "
        "and d.sent_at >= now() - make_interval(days => %s) and i.title is not null "
        "order by d.sent_at desc, di.position",
        (newsletter, days),
    ).fetchall()
    return [str(r["title"]) for r in rows]


def create_digest(
    conn: Connection,
    run_id: int,
    newsletter: str,
    *,
    body: dict[str, Any],
    model: str | None,
    prompt_version: str,
    fallback: bool,
    threshold: int,
    entries: list[tuple[int, int, bool, str | None]],
) -> int:
    """Store a digest and every item offered to it: each included item with its position, each
    left out with position -1 and the reason it was left out, and each item listed below the
    threshold with its place in the list and the reason BELOW_THRESHOLD.
    """
    row = conn.execute(
        "insert into digests (run_id, newsletter, body_json, model, prompt_version, fallback, "
        "threshold) values (%s, %s, %s, %s, %s, %s, %s) returning id",
        (run_id, newsletter, Jsonb(body), model, prompt_version, fallback, threshold),
    ).fetchone()
    assert row is not None
    digest_id = int(row["id"])
    for item_id, position, included, reason in entries:
        conn.execute(
            "insert into digest_items (digest_id, item_id, position, included, reason) "
            "values (%s, %s, %s, %s, %s)",
            (digest_id, item_id, position, included, reason),
        )
    return digest_id


def unsent_digests(conn: Connection, newsletters: list[str]) -> list[Row]:
    """Unsent digests of the given newsletters; a removed newsletter's digests stay unsent."""
    return conn.execute(
        "select id, run_id, newsletter, body_json, fallback, created_at from digests "
        "where sent_at is null and newsletter = any(%s) order by id",
        (newsletters,),
    ).fetchall()


def digest_entries(conn: Connection, digest_id: int) -> list[Row]:
    """The items a digest included, in reading order."""
    return _digest_items(conn, digest_id, listed=False)


def digest_listed(conn: Connection, digest_id: int) -> list[Row]:
    """The items a digest lists below the threshold, in the list's order."""
    return _digest_items(conn, digest_id, listed=True)


def _digest_items(conn: Connection, digest_id: int, *, listed: bool) -> list[Row]:
    return conn.execute(
        "select i.id, i.url, i.title, i.published_at, s.key as source_key, d.position, "
        "d.reason as offer_reason, a.score, a.reason from digest_items d "
        "join items i on i.id = d.item_id "
        "join sources s on s.id = i.source_id "
        "left join lateral (select score, reason from assessments where item_id = i.id "
        "order by created_at desc limit 1) a on true "
        "where d.digest_id = %s and d.included = %s and (d.included or d.reason = %s) "
        "order by d.position",
        (digest_id, not listed, BELOW_THRESHOLD),
    ).fetchall()


def mark_sent(conn: Connection, digest_id: int) -> None:
    conn.execute(
        "update digests set sent_at = now(), send_error = null where id = %s", (digest_id,)
    )


def mark_send_error(conn: Connection, digest_id: int, error: str) -> None:
    conn.execute(
        "update digests set send_error = %s where id = %s", (_safe(error[:1000]), digest_id)
    )


def save_rating(conn: Connection, rating: Rating, newsletters: list[str]) -> bool:
    """Store a rating unless its email was stored before, or its item isn't one that a digest of
    the given newsletters included or listed below the threshold; True when a row was added.
    """
    row = conn.execute(
        "insert into feedback (message_id, item_id, liked, sent_at) "
        "select %s, i.id, %s, %s from items i join digest_items d on d.item_id = i.id "
        "where i.id = %s and (d.included or d.reason = %s) and i.newsletter = any(%s) "
        "on conflict (message_id) do nothing returning id",
        (
            _safe(rating.message_id),
            rating.liked,
            rating.sent_at,
            rating.item_id,
            BELOW_THRESHOLD,
            newsletters,
        ),
    ).fetchone()
    return row is not None


def recent_ratings(
    conn: Connection, newsletter: str, days: int, per_source: int, per_side: int
) -> list[Row]:
    """A newsletter's latest rating of each item rated in the last `days` days, newest first: at
    most per_source liked and per_source disliked items from one source, and per_side of each in
    all, so one burst of ratings can't fill the list. Each row has its feedback id, title, liking
    and source key.
    """
    return conn.execute(
        "select id, title, liked, source_key from (select id, title, liked, source_key, sent_at, "
        "row_number() over (partition by liked order by sent_at desc) as n from (select *, "
        "row_number() over (partition by liked, source_key order by sent_at desc) as per_source "
        "from (select distinct on (f.item_id) f.id, i.title, f.liked, f.sent_at, "
        "s.key as source_key from feedback f join items i on i.id = f.item_id "
        "join sources s on s.id = i.source_id where i.newsletter = %s and i.title is not null "
        "and f.sent_at >= now() - make_interval(days => %s) "
        "order by f.item_id, f.sent_at desc) latest) by_source where per_source <= %s) ranked "
        "where n <= %s order by liked desc, sent_at desc",
        (newsletter, days, per_source, per_side),
    ).fetchall()


def health(conn: Connection, newsletter: str, source_keys: list[str]) -> Row:
    """One newsletter's failing and silent sources among source_keys, its items waiting to be
    fetched or scored and its unscorable items, the runs since its last sent digest, the owner's
    latest rating of each item, counted by source.
    """
    failing = conn.execute(
        "select key, consecutive_failures, last_error from sources "
        "where newsletter = %s and key = any(%s) and consecutive_failures >= 3 order by key",
        (newsletter, source_keys),
    ).fetchall()
    silent = conn.execute(
        "select key from sources where newsletter = %s and key = any(%s) "
        "and first_seen_at < now() - interval '14 days' "
        "and coalesce(last_new_item_at, first_seen_at) < now() - interval '14 days' "
        "order by key",
        (newsletter, source_keys),
    ).fetchall()
    waiting = conn.execute(
        "select count(*) as n from items i join sources s on s.id = i.source_id "
        "where i.newsletter = %s and s.key = any(%s) "
        "and i.state in ('new', 'enriched', 'enrich_failed')",
        (newsletter, source_keys),
    ).fetchone()
    failed_items = conn.execute(
        "select count(*) as n from items where newsletter = %s and state = 'assess_failed'",
        (newsletter,),
    ).fetchone()
    runs = conn.execute(
        "select count(*) as total, count(*) filter (where status = 'partial') as partial, "
        "count(*) filter (where status in ('failed', 'abandoned')) as failed from runs "
        "where id > coalesce((select max(run_id) from digests "
        "where newsletter = %s and sent_at is not null), 0)",
        (newsletter,),
    ).fetchone()
    ratings = conn.execute(
        "select s.key, count(*) filter (where latest.liked) as liked, "
        "count(*) filter (where not latest.liked) as disliked from (select distinct on "
        "(item_id) item_id, liked from feedback order by item_id, sent_at desc) latest "
        "join items i on i.id = latest.item_id join sources s on s.id = i.source_id "
        "where s.newsletter = %s and s.key = any(%s) group by s.key order by s.key",
        (newsletter, source_keys),
    ).fetchall()
    return {
        "failing_sources": failing,
        "silent_sources": [r["key"] for r in silent],
        "waiting": waiting["n"] if waiting else 0,
        "assess_failed": failed_items["n"] if failed_items else 0,
        "runs": runs or {"total": 0, "partial": 0, "failed": 0},
        "ratings": ratings,
    }


def backfill(
    conn: Connection, newsletter: str, source_key: str, since: datetime, limit: int
) -> int:
    """Move baseline items of one newsletter's source, published or first stored since `since`,
    back to new. Returns how many moved.
    """
    cur = conn.execute(
        "update items set state = 'new', queued_at = now() where id in (select i.id from items i "
        "join sources s on s.id = i.source_id where s.newsletter = %s and s.key = %s "
        "and i.state = 'baseline' and coalesce(i.published_at, i.first_seen_at) >= %s "
        "order by i.published_at desc nulls last, i.id desc limit %s)",
        (newsletter, source_key, since, limit),
    )
    conn.commit()
    return cur.rowcount


def last_profile_suggestion(conn: Connection, newsletter: str) -> datetime | None:
    row = conn.execute(
        "select max(created_at) as at from profile_suggestions where newsletter = %s",
        (newsletter,),
    ).fetchone()
    return row["at"] if row else None


def ratings_since(
    conn: Connection, newsletter: str, since: datetime | None, limit: int
) -> list[Row]:
    """A newsletter's latest rating of each item, among ratings stored after `since`, or all when
    it's None, newest first, with the item's title and source key.
    """
    return conn.execute(
        "select title, liked, source_key from (select distinct on (f.item_id) i.title, f.liked, "
        "f.sent_at, s.key as source_key from feedback f join items i on i.id = f.item_id "
        "join sources s on s.id = i.source_id where i.newsletter = %s and i.title is not null "
        "and (%s::timestamptz is null or f.read_at > %s) "
        "order by f.item_id, f.sent_at desc) latest order by sent_at desc limit %s",
        (newsletter, since, since, limit),
    ).fetchall()


def save_profile_suggestion(
    conn: Connection, newsletter: str, run_id: int, body: dict[str, Any]
) -> None:
    conn.execute(
        "insert into profile_suggestions (newsletter, run_id, body_json) values (%s, %s, %s)",
        (newsletter, run_id, Jsonb(body)),
    )
