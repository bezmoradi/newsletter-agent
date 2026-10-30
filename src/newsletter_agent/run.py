"""One scheduled run: lock, migrate, the steps in order, the runs row and the exit code."""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from datetime import UTC, datetime, timedelta

from newsletter_agent import mail, steps, store
from newsletter_agent.settings import MailSettings, Settings

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED_UNNOTIFIED = 1
EXIT_FAILED_NOTIFIED = 2
UNAVAILABLE_LIMIT = 3
ALL_SOURCES_FAILED_LIMIT = 3
STUCK_AFTER = timedelta(hours=3)
HARD_STOP_AFTER_DEADLINE = timedelta(minutes=15)


def failed_exit(mail_settings: MailSettings, reason: str) -> int:
    return (
        EXIT_FAILED_NOTIFIED
        if mail.notify_failure(mail_settings, reason)
        else EXIT_FAILED_UNNOTIFIED
    )


def run(settings: Settings) -> int:
    """Do one run and return the exit code: 0 for succeeded or partial, 2 for failed with the
    failure email sent, 1 for failed without it. A process still running 15 minutes past the run
    deadline exits with 1 on its own, so a hang can't hold the run lock.
    """
    watchdog = _start_watchdog(settings)
    try:
        return _run(settings)
    finally:
        watchdog.cancel()


def _start_watchdog(settings: Settings) -> threading.Timer:
    limit = timedelta(minutes=settings.run_deadline_minutes) + HARD_STOP_AFTER_DEADLINE

    def stop() -> None:
        log.error(
            "run passed its hard limit, so the process exits",
            extra={"fields": {"limit": str(limit)}},
        )
        os._exit(EXIT_FAILED_UNNOTIFIED)

    timer = threading.Timer(limit.total_seconds(), stop)
    timer.daemon = True
    timer.start()
    return timer


def _run(settings: Settings) -> int:
    with contextlib.ExitStack() as stack:
        try:
            conn, locked = stack.enter_context(store.locked(settings.database_url))
        except Exception as e:
            reason = f"the database is unreachable: {type(e).__name__}: {e}"
            log.error("run failed", extra={"fields": {"error": reason}})
            return failed_exit(settings.mail, reason)
        if not locked:
            return _lock_held(conn, settings)
        current: steps.Run | None = None
        status, error = "failed", None
        try:
            applied = store.migrate(conn)
            abandoned = store.abandon_unfinished_runs(conn)
            current = steps.Run(
                id=store.open_run(conn),
                deadline=datetime.now(UTC) + timedelta(minutes=settings.run_deadline_minutes),
            )
            log.info(
                "run started",
                extra={
                    "fields": {"run": current.id, "migrations": applied, "abandoned": abandoned}
                },
            )
            steps.read_ratings(conn, settings, current)
            steps.collect(conn, settings, current)
            steps.enrich(conn, settings, current)
            steps.assess(conn, settings, current)
            steps.digest(conn, settings, current)
            steps.deliver(conn, settings, current)
            status, error = _outcome(conn, current)
        except Exception as e:
            store.rollback_quietly(conn)
            log.exception("run crashed")
            error = f"{type(e).__name__}: {e}"

        notice_sent = status == "failed" and mail.notify_failure(
            settings.mail, error or "unknown error"
        )
        if current is not None:
            try:
                store.close_run(
                    conn,
                    current.id,
                    status=status,
                    counts=dict(current.counts),
                    usage=current.usage,
                    error=error,
                    claude_unavailable=current.claude_unavailable,
                    notice_sent=notice_sent,
                )
            except Exception:
                log.exception("could not record the end of the run")
            log.info(
                "run finished",
                extra={
                    "fields": {
                        "run": current.id,
                        "status": status,
                        "error": error,
                        "counts": dict(current.counts),
                        "usage": current.usage,
                    }
                },
            )
    if status != "failed":
        return EXIT_OK
    return EXIT_FAILED_NOTIFIED if notice_sent else EXIT_FAILED_UNNOTIFIED


def _lock_held(conn: store.Connection, settings: Settings) -> int:
    """Exit quietly while another run holds the lock, unless that run has gone on longer than any
    run can: then its process is stuck, and the owner gets the failure email every day until
    it's stopped.
    """
    started = store.unfinished_run_started(conn)
    limit = max(STUCK_AFTER, timedelta(minutes=settings.run_deadline_minutes + 30))
    if started is None or datetime.now(UTC) - started < limit:
        log.info("another run holds the lock, so this one does nothing")
        return EXIT_OK
    reason = (
        f"a run that started at {started:%Y-%m-%d %H:%M} UTC still holds the run lock, so no "
        "run can start; its container is probably stuck. On the Pi, remove it with "
        "`sudo docker rm -f newsletter-agent-run`"
    )
    log.error("run lock held by a stuck run", extra={"fields": {"started": started.isoformat()}})
    return failed_exit(settings.mail, reason)


def _outcome(conn: store.Connection, current: steps.Run) -> tuple[str, str | None]:
    if current.claude_auth_error:
        return "failed", f"Claude can't be used until this is fixed: {current.claude_auth_error}"
    if current.claude_unavailable:
        earlier = store.unavailable_streak(conn, current.id, UNAVAILABLE_LIMIT - 1)
        conn.commit()
        if earlier >= UNAVAILABLE_LIMIT - 1:
            return "failed", f"Claude was unavailable for {UNAVAILABLE_LIMIT} runs in a row"
    if current.all_sources_failed:
        earlier = store.all_sources_failed_streak(conn, current.id, ALL_SOURCES_FAILED_LIMIT - 1)
        conn.commit()
        if earlier >= ALL_SOURCES_FAILED_LIMIT - 1:
            return "failed", (
                f"every source failed in {ALL_SOURCES_FAILED_LIMIT} runs in a row; this run's "
                f"first errors: {'; '.join(current.source_errors)}"
            )
    return ("partial" if current.degraded else "succeeded"), None
