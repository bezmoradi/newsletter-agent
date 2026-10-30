"""All SQL. The only module that imports psycopg."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import datetime
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
    """Connect with a 10 s connect timeout and a 60 s statement timeout, over TLS only. Raises
    MalformedUrl, without the URL's text, when DATABASE_URL can't be parsed, because psycopg's own
    message would include the password, and InsecureConnection when the connection isn't
    encrypted.
    """
    try:
        conn = psycopg.connect(
            url,
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
        "published_at, summary, state) values (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "on conflict do nothing returning id",
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


def items_to_enrich(conn: Connection, newsletter: str, limit: int) -> list[Row]:
    return conn.execute(
        "select i.id, i.external_id, i.url, i.title, s.type as source_type from items i "
        "join sources s on s.id = i.source_id where i.newsletter = %s and i.state = 'new' "
        "order by coalesce(i.published_at, i.first_seen_at) desc, i.id desc limit %s",
        (newsletter, limit),
    ).fetchall()


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


def items_to_assess(conn: Connection, newsletter: str, limit: int, excerpt: int) -> list[Row]:
    return conn.execute(
        "select i.id, i.url, i.title, i.summary, i.published_at, s.key as source_key, "
        "left(c.text, %s) as excerpt from items i join sources s on s.id = i.source_id "
        "left join item_content c on c.item_id = i.id "
        "where i.newsletter = %s and i.state in ('enriched', 'enrich_failed') "
        "order by coalesce(i.published_at, i.first_seen_at) desc, i.id desc limit %s",
        (excerpt, newsletter, limit),
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
) -> None:
    conn.execute(
        "insert into assessments (item_id, model, prompt_version, profile_hash, score, reason, "
        "run_id) values (%s, %s, %s, %s, %s, %s, %s) on conflict do nothing",
        (item_id, model, prompt_version, profile_hash, score, _safe(reason), run_id),
    )
    conn.execute("update items set state = 'assessed' where id = %s", (item_id,))


def record_assess_failure(conn: Connection, item_id: int, max_attempts: int) -> bool:
    """Count a failed scoring attempt; True when this attempt made the item assess_failed."""
    row = conn.execute(
        "update items set assess_attempts = assess_attempts + 1, "
        "state = case when assess_attempts + 1 >= %s then 'assess_failed' else state end "
        "where id = %s returning state",
        (max_attempts, item_id),
    ).fetchone()
    return bool(row and row["state"] == "assess_failed")


def digest_candidates(conn: Connection, newsletter: str, threshold: int) -> list[Row]:
    """A newsletter's assessed items at or above threshold that were never offered to a digest,
    best first.
    """
    return conn.execute(
        "select * from (select distinct on (i.id) i.id, i.url, i.title, i.summary, "
        "i.published_at, s.key as source_key, a.score, a.reason, "
        "left(c.text, 1500) as excerpt from items i "
        "join sources s on s.id = i.source_id join assessments a on a.item_id = i.id "
        "left join item_content c on c.item_id = i.id "
        "left join digest_items d on d.item_id = i.id "
        "where i.newsletter = %s and i.state = 'assessed' and d.item_id is null "
        "order by i.id, a.created_at desc) latest where score >= %s "
        "order by score desc, published_at desc nulls last",
        (newsletter, threshold),
    ).fetchall()


def create_digest(
    conn: Connection,
    run_id: int,
    newsletter: str,
    *,
    body: dict[str, Any],
    model: str | None,
    prompt_version: str,
    fallback: bool,
    entries: list[tuple[int, int, bool]],
) -> int:
    row = conn.execute(
        "insert into digests (run_id, newsletter, body_json, model, prompt_version, fallback) "
        "values (%s, %s, %s, %s, %s, %s) returning id",
        (run_id, newsletter, Jsonb(body), model, prompt_version, fallback),
    ).fetchone()
    assert row is not None
    digest_id = int(row["id"])
    for item_id, position, included in entries:
        conn.execute(
            "insert into digest_items (digest_id, item_id, position, included) "
            "values (%s, %s, %s, %s)",
            (digest_id, item_id, position, included),
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
    return conn.execute(
        "select i.id, i.url, i.title, i.published_at, s.key as source_key, d.position, "
        "a.score, a.reason from digest_items d join items i on i.id = d.item_id "
        "join sources s on s.id = i.source_id "
        "left join lateral (select score, reason from assessments where item_id = i.id "
        "order by created_at desc limit 1) a on true "
        "where d.digest_id = %s and d.included order by d.position",
        (digest_id,),
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
    """Store a rating unless its email was stored before, its token names another database, or
    its item isn't one that a digest of the given newsletters included; True when a row was added.
    """
    row = conn.execute(
        "insert into feedback (message_id, item_id, liked, sent_at) "
        "select %s, i.id, %s, %s from items i join digest_items d on d.item_id = i.id "
        "where i.id = %s and d.included and i.newsletter = any(%s) "
        "and %s = (select feedback_token from instance) "
        "on conflict (message_id) do nothing returning id",
        (
            _safe(rating.message_id),
            rating.liked,
            rating.sent_at,
            rating.item_id,
            newsletters,
            rating.token,
        ),
    ).fetchone()
    return row is not None


def feedback_token(conn: Connection) -> str:
    """This database's token for rating links, made by migration 0004."""
    row = conn.execute("select feedback_token from instance").fetchone()
    assert row is not None
    return str(row["feedback_token"])


def recent_ratings(conn: Connection, newsletter: str, days: int, per_side: int) -> list[Row]:
    """A newsletter's latest rating of each item rated in the last `days` days: at most per_side
    liked and per_side disliked titles, newest first.
    """
    return conn.execute(
        "select title, liked from (select title, liked, sent_at, row_number() over "
        "(partition by liked order by sent_at desc) as n from (select distinct on (f.item_id) "
        "i.title, f.liked, f.sent_at from feedback f join items i on i.id = f.item_id "
        "where i.newsletter = %s and i.title is not null "
        "and f.sent_at >= now() - make_interval(days => %s) "
        "order by f.item_id, f.sent_at desc) latest) ranked "
        "where n <= %s order by liked desc, sent_at desc",
        (newsletter, days, per_side),
    ).fetchall()


def health(conn: Connection, newsletter: str, source_keys: list[str]) -> Row:
    """One newsletter's failing and silent sources among source_keys, its unscorable items, the
    runs since its last sent digest, and the owner's latest rating of each item, counted by
    source.
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
        "update items set state = 'new' where id in (select i.id from items i "
        "join sources s on s.id = i.source_id where s.newsletter = %s and s.key = %s "
        "and i.state = 'baseline' and coalesce(i.published_at, i.first_seen_at) >= %s "
        "order by i.published_at desc nulls last, i.id desc limit %s)",
        (newsletter, source_key, since, limit),
    )
    conn.commit()
    return cur.rowcount
