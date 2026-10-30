"""Gmail: rendering and sending the digest and failure emails over SMTP, and reading the owner's
ratings from the Sent folder over IMAP.
"""

from __future__ import annotations

import imaplib
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.errors import MessageError
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.policy import default as default_policy
from email.utils import parsedate_to_datetime
from html import escape
from typing import Any
from urllib.parse import quote

from newsletter_agent.settings import MailSettings, revision
from newsletter_agent.text import defang
from newsletter_agent.urls import domain, is_web_url

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_TIMEOUT = 60
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
IMAP_TIMEOUT = 60
TOKEN_WARNING_DAYS = 30
MUTED = "color: #59636e;"
LINK = "color: #0969da;"
RATING_HELP = (
    "Each rating link opens an email to yourself with the rating in its subject. Send it, and "
    "the next run reads it from your Sent folder."
)
FEEDBACK_SUBJECT = "newsletter-agent feedback"
MAX_FEEDBACK_MESSAGES = 500
_FEEDBACK_RE = re.compile(
    rf"^{re.escape(FEEDBACK_SUBJECT)} (more|less) ([1-9][0-9]{{0,17}}) ([0-9a-f]{{12}})$",
    re.IGNORECASE,
)
_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) (?:NIL|"(?:[^"\\]|\\.)*") (?P<name>.+)$')
_CODE_RE = re.compile(r"`([^`]+)`")


@dataclass(frozen=True)
class Email:
    subject: str
    text: str
    html: str | None = None


@dataclass(frozen=True)
class Rating:
    """One "More like this" or "Less like this" email the owner sent. token names the database
    whose digest the link came from.
    """

    message_id: str
    item_id: int
    liked: bool
    sent_at: datetime
    token: str


@dataclass(frozen=True)
class _Item:
    title: str
    url: str | None
    domain: str | None
    source: str
    score: int | None
    summary: str
    more: str | None
    less: str | None

    @property
    def meta(self) -> str:
        parts = [
            self.domain,
            self.source,
            f"score {self.score}/10" if self.score is not None else None,
        ]
        return " · ".join(p for p in parts if p)


def render_digest(
    *,
    name: str,
    intro: str | None,
    entries: list[dict[str, Any]],
    summaries: dict[int, str],
    source_names: dict[str, str],
    fallback: bool,
    health: dict[str, Any] | None,
    feedback_to: str | None,
    feedback_token: str,
    token_days_left: int,
    today: date,
) -> Email:
    """Build the digest email. Every title, summary and the intro go through text.defang, every
    HTML value is escaped, and links come only from stored item URLs, plus, when feedback_to is
    set, the rating links built by feedback_link. With health set to None, the email has no health
    footer and no token warning.
    """

    def rating(item_id: int, *, liked: bool) -> str | None:
        if not feedback_to:
            return None
        return feedback_link(feedback_to, item_id, feedback_token, liked=liked)

    items = [
        _Item(
            title=defang(entry["title"] or "(no title)"),
            url=entry["url"] if is_web_url(entry["url"]) else None,
            domain=domain(entry["url"]) if is_web_url(entry["url"]) else None,
            source=source_names.get(entry["source_key"], entry["source_key"]),
            score=entry["score"],
            summary=defang(summaries.get(entry["id"]) or entry["reason"] or ""),
            more=rating(entry["id"], liked=True),
            less=rating(entry["id"], liked=False),
        )
        for entry in entries
    ]
    opening = (
        "Claude couldn't write today's summaries, so each item shows the reason it was picked."
        if fallback
        else defang(intro)
        if intro
        else None
    )
    warning = (
        f"The Claude token expires in {token_days_left} days. Run `claude setup-token` and "
        "replace CLAUDE_CODE_OAUTH_TOKEN on the Pi."
        if health is not None and token_days_left <= TOKEN_WARNING_DAYS
        else None
    )
    footer = _health_lines(health, token_days_left) if health is not None else []
    if feedback_to and items:
        footer.append(RATING_HELP)
    heading = f"{name}, {today.day} {today:%B %Y}"
    count = len(items)
    return Email(
        subject=f"{name}: {count} item{'' if count == 1 else 's'} ({today:%Y-%m-%d})",
        text=_text(heading, warning, opening, items, footer),
        html=_html(heading, warning, opening, items, footer),
    )


def feedback_link(address: str, item_id: int, token: str, *, liked: bool) -> str:
    """A mailto: link that opens an email to address whose subject rates the item. The token
    names the database, so a rating from another database's digest is never stored here.
    """
    subject = f"{FEEDBACK_SUBJECT} {'more' if liked else 'less'} {item_id} {token}"
    return f"mailto:{quote(address, safe='@')}?subject={quote(subject, safe='')}"


def _health_lines(health: dict[str, Any], token_days_left: int) -> list[str]:
    runs = health["runs"]
    lines = [
        f"Runs since the last digest: {runs['total']} ({runs['partial']} partial, "
        f"{runs['failed']} failed). Claude token: {token_days_left} days left."
    ]
    if health["failing_sources"]:
        failing = "; ".join(
            f"{s['key']} ({defang(s['last_error'] or '')})" for s in health["failing_sources"]
        )
        lines.append(f"Failing for 3 or more runs: {failing}")
    if health["silent_sources"]:
        lines.append(f"No new items in 14 days: {', '.join(health['silent_sources'])}")
    if health["waiting"]:
        lines.append(f"Items waiting to be fetched or scored: {health['waiting']}")
    if health["assess_failed"]:
        lines.append(f"Items Claude couldn't score after 3 tries: {health['assess_failed']}")
    if health["ratings"]:
        ratings = "; ".join(
            f"{r['key']} {r['liked']} more, {r['disliked']} less" for r in health["ratings"]
        )
        lines.append(f"Your ratings by source: {ratings}")
    if health["ratings_error"]:
        lines.append(
            f"Your ratings couldn't be read from Gmail this run: {defang(health['ratings_error'])}"
        )
    return lines


def _text(
    heading: str, warning: str | None, opening: str | None, items: list[_Item], footer: list[str]
) -> str:
    out = []
    if warning:
        out += [f"WARNING: {warning}", ""]
    out += [heading, ""]
    if opening:
        out += [opening, ""]
    for n, item in enumerate(items, 1):
        out += [f"{n}. {item.title}", f"   {item.meta}", f"   {item.summary}"]
        if item.url:
            out.append(f"   {item.url}")
        if item.more and item.less:
            out += [f"   More like this: {item.more}", f"   Less like this: {item.less}"]
        out.append("")
    if footer:
        out += ["--", *footer]
    return "\n".join(out).rstrip() + "\n"


def _html(
    heading: str,
    warning: str | None,
    opening: str | None,
    items: list[_Item],
    footer: list[str],
) -> str:
    out = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="color-scheme" content="light">',
        '<meta name="supported-color-schemes" content="light">',
        f"<title>{escape(heading)}</title></head>",
        '<body style="font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif; '
        "max-width: 640px; margin: 0 auto; padding: 16px; color: #1f2328; background: #fff; "
        'line-height: 1.5; overflow-wrap: anywhere; word-break: break-word;">',
    ]
    if warning:
        banner = _CODE_RE.sub(r"<code>\1</code>", escape(warning))
        out.append(
            '<p style="background: #fff8c5; border: 1px solid #d4a72c; padding: 8px 12px;">'
            f"<strong>{banner}</strong></p>"
        )
    out.append(f'<h1 style="font-size: 20px;">{escape(heading)}</h1>')
    if opening:
        out.append(f"<p>{escape(opening)}</p>")
    for n, item in enumerate(items, 1):
        title = escape(item.title)
        if item.url:
            title = (
                f'<a href="{escape(item.url, quote=True)}" '
                f'style="{LINK} text-decoration: none;">{title}</a>'
            )
        out += [
            '<div style="margin: 20px 0;">',
            f'<h2 style="margin: 0; font-size: 16px; font-weight: 600;">{n}. {title}</h2>',
            f'<p style="margin: 2px 0; font-size: 13px; {MUTED}">{escape(item.meta)}</p>',
            f'<p style="margin: 4px 0 0;">{escape(item.summary)}</p>',
        ]
        if item.more and item.less:
            link = (
                f'style="{LINK} text-decoration: underline; display: inline-block; '
                'padding: 8px 0; margin-right: 24px;"'
            )
            out.append(
                '<p style="margin: 0; font-size: 15px;">'
                f'<a href="{escape(item.more, quote=True)}" {link}>More like this</a>'
                f'<a href="{escape(item.less, quote=True)}" {link}>Less like this</a></p>'
            )
        out.append("</div>")
    if footer:
        out.append('<hr style="border: none; border-top: 1px solid #d1d9e0; margin: 24px 0 12px;">')
    out += [
        f'<p style="margin: 4px 0; font-size: 12px; {MUTED}">{escape(line)}</p>' for line in footer
    ]
    out.append("</body></html>")
    return "\n".join(out)


def render_failure(reason: str, today: date) -> Email:
    text = "\n".join(
        [
            f"The newsletter agent run on {today:%Y-%m-%d} failed.",
            "",
            f"Reason: {defang(reason)}",
            "",
            "On the Pi, see the log with:",
            "  sudo journalctl -u newsletter-agent --since today",
            "",
            "and the latest runs with:",
            "  cd /opt/newsletter-agent",
            "  sudo docker compose exec db psql -U newsletter_agent \\",
            '    -c "select id, started_at, status, error from runs order by id desc limit 5"',
            "",
            f"Revision: {revision()}",
        ]
    )
    return Email(subject=f"Newsletter agent failed ({today:%Y-%m-%d})", text=text + "\n")


def send(email: Email, settings: MailSettings, to: str) -> None:
    """Send from SMTP_USERNAME to `to` through smtp.gmail.com:587 with STARTTLS and a 60 s
    timeout. Raises on failure.
    """
    message = EmailMessage()
    message["From"] = settings.smtp_username
    message["To"] = to
    message["Subject"] = email.subject
    message.set_content(email.text)
    if email.html is not None:
        message.add_alternative(email.html, subtype="html")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(settings.smtp_username, settings.smtp_app_password)
        smtp.send_message(message)


def error_summary(error: Exception) -> str:
    """The error as it may be logged or stored. SMTP errors quote email addresses, so they give
    only their type and SMTP reply codes; any other error gives its type and message.
    """
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        codes = sorted({str(code) for code, _ in error.recipients.values()})
        return " ".join([type(error).__name__, *codes])
    if isinstance(error, smtplib.SMTPResponseException):
        return f"{type(error).__name__} {error.smtp_code}"
    if isinstance(error, smtplib.SMTPException):
        return type(error).__name__
    return f"{type(error).__name__}: {error}"


def notify_failure(settings: MailSettings, reason: str) -> bool:
    """Send the failure email to SMTP_USERNAME; True when it went out. Never raises."""
    try:
        send(render_failure(reason, date.today()), settings, settings.smtp_username)
    except Exception as e:
        log.error("failure email not sent", extra={"fields": {"error": error_summary(e)}})
        return False
    log.info("failure email sent")
    return True


def check_login(settings: MailSettings) -> None:
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(settings.smtp_username, settings.smtp_app_password)


def read_ratings(settings: MailSettings, since: date) -> list[Rating]:
    """Read the ratings SMTP_USERNAME sent since `since`, from the headers of the newest 500
    feedback emails in its Gmail Sent folder. Signs in to imap.gmail.com:993 over TLS with a 60 s
    timeout, opens the folder read-only, and fetches only Subject, Date and Message-ID, never a
    body. A message whose subject, Date or Message-ID doesn't parse is left out. Raises on failure.
    """
    with imaplib.IMAP4_SSL(
        IMAP_HOST, IMAP_PORT, ssl_context=ssl.create_default_context(), timeout=IMAP_TIMEOUT
    ) as imap:
        imap.login(settings.smtp_username, settings.smtp_app_password)
        _ok(imap.select(_sent_folder(imap), readonly=True), "EXAMINE")
        found = _ok(
            imap.search(None, "SINCE", f"{since:%d-%b-%Y}", "SUBJECT", f'"{FEEDBACK_SUBJECT}"'),
            "SEARCH",
        )
        numbers = (found[0] or b"").split()[-MAX_FEEDBACK_MESSAGES:]
        if not numbers:
            return []
        fetched = _ok(
            imap.fetch(
                b",".join(numbers).decode(),
                "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE MESSAGE-ID)])",
            ),
            "FETCH",
        )
    parser = BytesHeaderParser(policy=default_policy)
    ratings = (_rating(parser.parsebytes(part[1])) for part in fetched if isinstance(part, tuple))
    return [rating for rating in ratings if rating is not None]


def _ok(response: tuple[str, list[Any]], command: str) -> list[Any]:
    status, data = response
    if status != "OK":
        raise imaplib.IMAP4.error(f"IMAP {command} answered {status}: {data!r}")
    return data


def _sent_folder(imap: imaplib.IMAP4) -> str:
    for line in _ok(imap.list(), "LIST"):
        match = _LIST_RE.match(line) if isinstance(line, bytes) else None
        if match and b"\\sent" in match["flags"].lower().split():
            return match["name"].decode()
    raise imaplib.IMAP4.error("no IMAP folder has the \\Sent attribute")


def _rating(headers: Any) -> Rating | None:
    try:
        match = _FEEDBACK_RE.match(" ".join(str(headers.get("Subject", "")).split()))
        message_id = str(headers.get("Message-ID", "")).strip()
        sent_at = parsedate_to_datetime(str(headers.get("Date", "")))
    except (AttributeError, IndexError, TypeError, ValueError, MessageError):
        return None
    if match is None or not message_id:
        return None
    return Rating(
        message_id=message_id[:500],
        item_id=int(match[2]),
        liked=match[1].lower() == "more",
        sent_at=sent_at if sent_at.tzinfo else sent_at.replace(tzinfo=UTC),
        token=match[3].lower(),
    )
