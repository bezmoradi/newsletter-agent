"""Command-line entry point: run, send-pending, migrate, check, backfill, notify-failure and
authorize-youtube.

notify-failure needs only the settings and mail modules, so the other commands import the rest
when they run: a fault that breaks importing the run can't also stop the failure email.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from newsletter_agent import mail
from newsletter_agent.settings import (
    ConfigError,
    Settings,
    load_mail_settings,
    load_settings,
    recipient_var,
    revision,
    take_secrets,
)

if TYPE_CHECKING:
    from newsletter_agent import store

log = logging.getLogger("newsletter_agent")

CHECKS = ("db", "smtp", "imap", "youtube", "likes", "claude", "websearch")
WEB_SEARCH_CHECK_TERM = "artificial intelligence"
CHECK_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for noisy in ("urllib3", "claude_agent_sdk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="newsletter-agent",
        description="Collect news, have Claude score it, and email one digest per newsletter.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="collect, score, write and email the digests")
    sub.add_parser("send-pending", help="email digests that failed to send earlier")
    sub.add_parser("migrate", help="apply pending database migrations now, as part of a deploy")
    check = sub.add_parser("check", help="test the configuration and every connection")
    check.add_argument(
        "--skip", action="append", choices=CHECKS, default=[], help="skip one check; repeatable"
    )
    backfill = sub.add_parser("backfill", help="queue baseline items of one source for scoring")
    backfill.add_argument("--newsletter", required=True, help="the newsletter's folder name")
    backfill.add_argument("--source", required=True, help="the source key in its newsletter.toml")
    backfill.add_argument(
        "--since", required=True, type=date.fromisoformat, help="a date like 2026-10-01"
    )
    backfill.add_argument(
        "--limit", type=_positive_int, help="at most this many; default MAX_ITEMS_PER_RUN"
    )
    sub.add_parser("notify-failure", help="email a notice that the scheduled run died")
    authorize = sub.add_parser(
        "authorize-youtube",
        help="sign in to Google once, in a browser, so the agent can read your YouTube likes",
    )
    authorize.add_argument(
        "--client-file", required=True, type=Path, help="the Desktop app client's JSON file"
    )
    authorize.add_argument(
        "--env-file", required=True, type=Path, help="the agent.env to save the sign-in to"
    )
    return parser


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {number}")
    return number


def main(argv: list[str] | None = None) -> int:
    """Parse the command line, run the command and return the process exit code."""
    args = _parser().parse_args(argv)
    _configure_logging()
    secrets = take_secrets(os.environ)
    config_dir = Path(os.environ.get("NEWSLETTER_AGENT_CONFIG_DIR", "config"))

    if args.command == "authorize-youtube":
        return _authorize_youtube(args.client_file, args.env_file)

    if args.command == "notify-failure":
        try:
            mail_settings = load_mail_settings(secrets)
        except ConfigError as e:
            log.error("cannot send the failure email", extra={"fields": {"error": str(e)}})
            return 1
        reason = (
            "the scheduled run stopped before it could report: "
            "it crashed, timed out or its container didn't start"
        )
        return 0 if mail.notify_failure(mail_settings, reason) else 1

    from newsletter_agent import run as run_mod

    try:
        settings = load_settings(os.environ, secrets, config_dir)
    except ConfigError as e:
        log.error("invalid configuration", extra={"fields": {"error": str(e)}})
        if args.command != "run":
            return 1
        try:
            return run_mod.failed_exit(load_mail_settings(secrets), f"invalid configuration: {e}")
        except ConfigError:
            return run_mod.EXIT_FAILED_UNNOTIFIED

    if args.command == "run":
        return run_mod.run(settings)
    if args.command == "send-pending":
        return _send_pending(settings)
    if args.command == "migrate":
        return _migrate(settings)
    if args.command == "check":
        return _check(settings, set(args.skip))
    return _backfill(
        settings,
        args.newsletter,
        args.source,
        args.since,
        settings.max_items_per_run if args.limit is None else args.limit,
    )


def _authorize_youtube(client_file: Path, env_file: Path) -> int:
    from newsletter_agent import http, youtube_auth

    try:
        client = youtube_auth.load_client(client_file)
        print("Opening Google's sign-in in your browser; waiting up to 10 minutes.")
        refresh_token = youtube_auth.authorize(client)
    except (OSError, ValueError, RuntimeError, http.FetchError) as e:
        print(f"Sign-in failed: {type(e).__name__}: {e}")
        return 1
    values = {
        "YOUTUBE_OAUTH_CLIENT_ID": client.client_id,
        "YOUTUBE_OAUTH_CLIENT_SECRET": client.client_secret,
        "YOUTUBE_OAUTH_REFRESH_TOKEN": refresh_token,
    }
    _save_env(env_file, values)
    print(f"Saved {', '.join(values)} to {env_file}; the values aren't shown.")
    return 0


def _save_env(path: Path, values: dict[str, str]) -> None:
    """Set the variables in an env file, replacing their earlier lines, and keep the file
    readable by its owner only.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    kept = [line for line in lines if line.split("=", 1)[0].strip() not in values]
    kept += [f"{name}={value}" for name, value in values.items()]
    staged = path.with_name(path.name + ".new")
    staged.touch(mode=0o600)
    staged.chmod(0o600)
    staged.write_text("\n".join(kept) + "\n", encoding="utf-8")
    staged.replace(path)


@contextmanager
def _maintenance(settings: Settings) -> Iterator[store.Connection | None]:
    """The run lock and a migrated database for a command run by hand, after printing the
    migrations it applied; None, after saying so, while a run holds the lock.
    """
    from newsletter_agent import store

    with store.locked(settings.database_url) as (conn, held):
        if not held:
            print("a run is in progress; try again when it ends")
            yield None
            return
        applied = store.migrate(conn)
        print(f"applied migrations: {', '.join(applied)}" if applied else "no pending migrations")
        yield conn


def _migrate(settings: Settings) -> int:
    with _maintenance(settings) as conn:
        held = conn is not None
    return 0 if held else 1


def _send_pending(settings: Settings) -> int:
    from newsletter_agent import steps, store

    with _maintenance(settings) as conn:
        if conn is None:
            return 1
        sent = steps.deliver(conn, settings, None)
        left = len(store.unsent_digests(conn, [n.key for n in settings.newsletters]))
    print(f"sent {sent} digest(s); {left} still unsent")
    return 0 if left == 0 else 1


def _backfill(settings: Settings, newsletter_key: str, source: str, since: date, limit: int) -> int:
    newsletter = settings.newsletter(newsletter_key)
    if newsletter is None:
        print(f"no newsletter folder named {newsletter_key!r}")
        return 1
    if source not in {s.key for s in newsletter.sources}:
        print(f"no enabled source with key {source!r} in newsletter {newsletter_key!r}")
        return 1
    from newsletter_agent import store

    with _maintenance(settings) as conn:
        if conn is None:
            return 1
        moved = store.backfill(
            conn, newsletter_key, source, datetime.combine(since, time.min, UTC), limit
        )
    print(f"queued {moved} item(s) from {newsletter_key}/{source} for the next run")
    return 0


def _check_youtube_key(settings: Settings, report: Callable[[str, bool, str], None]) -> None:
    from newsletter_agent.sources import youtube_search

    if settings.youtube_api_key is None:
        report("youtube", True, "no API key set and no youtube_search source")
        return
    try:
        youtube_search.check_key(settings.youtube_api_key)
        report("youtube", True, "the API key works")
    except Exception as e:
        report("youtube", False, f"{type(e).__name__}: {e}")


def _check_likes(settings: Settings, report: Callable[[str, bool, str], None]) -> None:
    from newsletter_agent import http, steps, youtube_auth
    from newsletter_agent.sources import youtube_likes

    account = settings.youtube_account
    if account is None:
        report("likes", True, "no youtube_likes source")
        return
    try:
        client = youtube_auth.Client(account.client_id, account.client_secret)
        token = youtube_auth.access_token(client, account.refresh_token)
        since = datetime.now(UTC) - timedelta(days=steps.LIKES_DAYS)
        likes = youtube_likes.recent(token, since, deadline=float("inf"))
        report("likes", True, f"signed in; {len(likes)} likes in the last {steps.LIKES_DAYS} days")
    except (http.FetchError, ValueError) as e:
        report("likes", False, f"{type(e).__name__}: {e}")


def _check(settings: Settings, skip: set[str]) -> int:
    from newsletter_agent import llm, steps, store
    from newsletter_agent.sources import web_search

    failures = 0

    def report(name: str, ok: bool, detail: str) -> None:
        nonlocal failures
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")

    report(
        "config",
        True,
        f"revision {revision()}; {len(settings.newsletters)} newsletter(s); score model "
        f"{settings.score_model}, "
        f"digest model {settings.digest_model}; transcripts "
        f"{'on' if settings.transcripts_enabled else 'off'}; YouTube search terms "
        f"{sum(n.search_terms('youtube_search') for n in settings.newsletters)} of "
        f"{settings.youtube_search_max_queries}; web search terms "
        f"{sum(n.search_terms('web_search') for n in settings.newsletters)} of "
        f"{settings.web_search_max_queries}, model {settings.search_model}; Claude token "
        f"{settings.token_days_left(date.today())} days left",
    )
    for newsletter in settings.newsletters:
        recipient = (
            "SMTP_USERNAME (you)"
            if settings.is_owner(newsletter)
            else recipient_var(newsletter.key)
        )
        report(
            f"newsletter {newsletter.key}",
            True,
            f'"{newsletter.name}"; {len(newsletter.sources)} enabled sources; '
            f"{newsletter.search_terms('youtube_search')} YouTube and "
            f"{newsletter.search_terms('web_search')} web search terms; "
            f"threshold {newsletter.score_threshold}; "
            f"sent to {recipient}",
        )
    if "db" not in skip:
        try:
            with store.connect(settings.database_url) as conn:
                pending = store.pending_migrations(conn)
            report("db", True, f"connected; {len(pending)} pending migration(s)")
        except Exception as e:
            report("db", False, f"{type(e).__name__}: {e}")
    if "smtp" not in skip:
        try:
            mail.check_login(settings.mail)
            report("smtp", True, f"signed in to {mail.SMTP_HOST}")
        except Exception as e:
            report("smtp", False, f"{type(e).__name__}: {e}")
    if "imap" not in skip:
        if not any(settings.is_owner(n) for n in settings.newsletters):
            report("imap", True, "no newsletter goes to SMTP_USERNAME, so no ratings are read")
        else:
            days = steps.RATINGS_READ_DAYS
            try:
                ratings = mail.read_ratings(settings.mail, date.today() - timedelta(days=days))
                report(
                    "imap",
                    True,
                    f"read the Sent folder on {mail.IMAP_HOST}; {len(ratings)} rating(s) "
                    f"sent in the last {days} days",
                )
            except Exception as e:
                report("imap", False, f"{type(e).__name__}: {e}")
    if "youtube" not in skip:
        _check_youtube_key(settings, report)
    if "likes" not in skip:
        _check_likes(settings, report)
    if "claude" not in skip:
        for model in sorted({settings.score_model, settings.digest_model}):
            try:
                result = llm.structured("Return ok set to true.", CHECK_SCHEMA, model, timeout=180)
                report(
                    f"claude {model}",
                    result.output.get("ok") is True,
                    f"answered; tools offered {list(result.tools)}; usage "
                    f"{result.usage.get('input_tokens', '?')} input and "
                    f"{result.usage.get('output_tokens', '?')} output tokens",
                )
            except llm.ClaudeError as e:
                report(f"claude {model}", False, f"{type(e).__name__}: {e}")
    if "websearch" not in skip:
        if not any(n.search_terms("web_search") for n in settings.newsletters):
            report("websearch", True, "no web_search source")
        else:
            try:
                result, candidates = web_search.search(
                    WEB_SEARCH_CHECK_TERM, 7, settings.search_model
                )
                report(
                    f"websearch {result.model}",
                    True,
                    f"searched; tools offered {list(result.tools)}; returned "
                    f"{len(candidates)} links for {WEB_SEARCH_CHECK_TERM!r}",
                )
            except llm.ClaudeError as e:
                report("websearch", False, f"{type(e).__name__}: {e}")
    return 0 if failures == 0 else 1
