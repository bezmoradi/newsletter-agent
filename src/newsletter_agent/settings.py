"""Settings from the environment and config/, with secrets kept out of Claude's reach."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SECRET_VARS = (
    "DATABASE_URL",
    "SMTP_USERNAME",
    "SMTP_APP_PASSWORD",
    "YOUTUBE_API_KEY",
    "POSTGRES_PASSWORD",
    "AGENT_DB_PASSWORD",
)
RECIPIENT_PREFIX = "DIGEST_TO_"
TOKEN_LIFETIME_DAYS = 365
DEFAULT_MODEL = "claude-opus-5-5"
NEWSLETTERS_DIR = "newsletters"
NEWSLETTER_FILE = "newsletter.toml"
INTERESTS_FILE = "interests.md"
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_CHANNEL_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
SOURCE_TYPES = ("feed", "sitemap", "youtube", "youtube_search", "web_search")
SEARCH_TYPES = ("youtube_search", "web_search")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class SourceConfig:
    key: str
    type: str
    url: str | None = None
    channel_id: str | None = None
    queries: tuple[str, ...] = ()
    include: tuple[re.Pattern[str], ...] = ()
    name: str | None = None

    @property
    def label(self) -> str:
        return self.name or self.key

    @property
    def definition(self) -> str:
        """What decides which entries the source yields; a change means a new baseline."""
        return json.dumps([self.type, self.url, self.channel_id, [p.pattern for p in self.include]])

    def matches(self, path: str) -> bool:
        return not self.include or any(p.search(path) for p in self.include)


@dataclass(frozen=True)
class NewsletterConfig:
    key: str
    name: str
    recipient: str = field(repr=False)
    score_threshold: int
    sources: tuple[SourceConfig, ...]
    interests: str = field(repr=False)

    @property
    def profile_hash(self) -> str:
        return hashlib.sha256(self.interests.encode()).hexdigest()[:16]

    def search_terms(self, source_type: str) -> int:
        return sum(len(s.queries) for s in self.sources if s.type == source_type)


@dataclass(frozen=True)
class MailSettings:
    smtp_username: str = field(repr=False)
    smtp_app_password: str = field(repr=False)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    mail: MailSettings
    youtube_api_key: str | None = field(repr=False)
    score_model: str
    digest_model: str
    search_model: str
    score_batch_size: int
    max_items_per_run: int
    digest_max_items: int
    backfill_days: int
    youtube_search_max_queries: int
    web_search_max_queries: int
    transcripts_enabled: bool
    token_created: date
    run_deadline_minutes: int
    newsletters: tuple[NewsletterConfig, ...]

    def token_days_left(self, today: date) -> int:
        return TOKEN_LIFETIME_DAYS - (today - self.token_created).days

    def newsletter(self, key: str) -> NewsletterConfig | None:
        return next((n for n in self.newsletters if n.key == key), None)

    def is_owner(self, newsletter: NewsletterConfig) -> bool:
        return newsletter.recipient.casefold() == self.mail.smtp_username.casefold()


def recipient_var(newsletter_key: str) -> str:
    return RECIPIENT_PREFIX + newsletter_key.upper().replace("-", "_")


def take_secrets(environ: MutableMapping[str, str]) -> dict[str, str | None]:
    """Remove every secret variable, including each DIGEST_TO_<KEY> address, from environ and
    return them. CLAUDE_CODE_OAUTH_TOKEN stays, because Claude Code needs it.
    """
    names = [*SECRET_VARS, *(name for name in environ if name.startswith(RECIPIENT_PREFIX))]
    return {name: environ.pop(name, None) or None for name in names}


def load_mail_settings(secrets: dict[str, str | None]) -> MailSettings:
    """Build only the email settings, so a failure email can go out even when the rest of the
    configuration is invalid. Raises ConfigError.
    """
    return MailSettings(
        smtp_username=_require(secrets, "SMTP_USERNAME"),
        smtp_app_password=_require(secrets, "SMTP_APP_PASSWORD"),
    )


def load_settings(
    environ: MutableMapping[str, str], secrets: dict[str, str | None], config_dir: Path
) -> Settings:
    """Validate every newsletter under config/newsletters/ and the environment before any
    network call. Raises ConfigError naming the problem, never a secret value.
    """
    mail = load_mail_settings(secrets)
    youtube_cap = _int(environ, "YOUTUBE_SEARCH_MAX_QUERIES", 10, low=1, high=100)
    web_cap = _int(environ, "WEB_SEARCH_MAX_QUERIES", 10, low=1, high=50)
    default_threshold = _int(environ, "SCORE_THRESHOLD", 6, low=0, high=10)
    root = config_dir / NEWSLETTERS_DIR
    folders = (
        sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
        if root.is_dir()
        else []
    )
    if not folders:
        raise ConfigError(f"no newsletter folders in {root}")
    newsletters = tuple(
        _load_newsletter(folder, secrets, mail.smtp_username, default_threshold)
        for folder in folders
    )

    youtube_terms = sum(n.search_terms("youtube_search") for n in newsletters)
    if youtube_terms > youtube_cap:
        raise ConfigError(
            f"{youtube_terms} YouTube search terms across all newsletters, more than "
            f"YOUTUBE_SEARCH_MAX_QUERIES ({youtube_cap})"
        )
    web_terms = sum(n.search_terms("web_search") for n in newsletters)
    if web_terms > web_cap:
        raise ConfigError(
            f"{web_terms} web search terms across all newsletters, more than "
            f"WEB_SEARCH_MAX_QUERIES ({web_cap})"
        )
    youtube_key = secrets.get("YOUTUBE_API_KEY")
    if youtube_key is None and youtube_terms:
        raise ConfigError(
            "YOUTUBE_API_KEY is not set, and a newsletter has a youtube_search source"
        )

    return Settings(
        database_url=_require(secrets, "DATABASE_URL"),
        mail=mail,
        youtube_api_key=youtube_key,
        score_model=environ.get("SCORE_MODEL") or DEFAULT_MODEL,
        digest_model=environ.get("DIGEST_MODEL") or DEFAULT_MODEL,
        search_model=environ.get("SEARCH_MODEL") or DEFAULT_MODEL,
        score_batch_size=_int(environ, "SCORE_BATCH_SIZE", 20, low=1, high=50),
        max_items_per_run=_int(environ, "MAX_ITEMS_PER_RUN", 100, low=1),
        digest_max_items=_int(environ, "DIGEST_MAX_ITEMS", 15, low=1),
        backfill_days=_int(environ, "BACKFILL_DAYS", 7, low=0),
        youtube_search_max_queries=youtube_cap,
        web_search_max_queries=web_cap,
        transcripts_enabled=_bool(environ, "TRANSCRIPTS_ENABLED", True),
        token_created=_date(environ, "TOKEN_CREATED", date(2026, 10, 30)),
        run_deadline_minutes=_int(environ, "RUN_DEADLINE_MINUTES", 90, low=1, high=104),
        newsletters=newsletters,
    )


def _load_newsletter(
    folder: Path, secrets: dict[str, str | None], owner: str, default_threshold: int
) -> NewsletterConfig:
    key = folder.name
    if not _KEY_RE.match(key):
        raise ConfigError(f"newsletter folder {folder} must be named like {_KEY_RE.pattern}")
    path = folder / NEWSLETTER_FILE
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from None

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{path}: 'name' must be a non-empty string")
    threshold = raw.get("score_threshold", default_threshold)
    if not isinstance(threshold, int) or isinstance(threshold, bool) or not 0 <= threshold <= 10:
        raise ConfigError(f"{path}: 'score_threshold' must be a whole number from 0 to 10")

    interests_path = folder / INTERESTS_FILE
    try:
        interests = interests_path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ConfigError(f"cannot read {interests_path}: {e.strerror}") from None
    if not interests:
        raise ConfigError(f"{interests_path} is empty")

    return NewsletterConfig(
        key=key,
        name=name.strip(),
        recipient=secrets.get(recipient_var(key)) or owner,
        score_threshold=threshold,
        sources=_parse_sources(raw.get("sources"), path),
        interests=interests,
    )


def _parse_sources(entries: Any, path: Path) -> tuple[SourceConfig, ...]:
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{path} must have [[sources]] entries")
    seen: set[str] = set()
    result = []
    for i, entry in enumerate(entries):
        where = f"{path} source #{i + 1}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be a table")
        key = entry.get("key")
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise ConfigError(f"{where}: 'key' must match {_KEY_RE.pattern}")
        where = f"{path} source '{key}'"
        if key in seen:
            raise ConfigError(f"{where}: duplicate key")
        seen.add(key)
        if entry.get("enabled", True) is False:
            continue

        kind = entry.get("type")
        if kind not in SOURCE_TYPES:
            raise ConfigError(f"{where}: unknown type {kind!r}; known: {sorted(SOURCE_TYPES)}")
        url = entry.get("url")
        channel_id = entry.get("channel_id")
        queries = entry.get("queries") or []
        if kind in ("feed", "sitemap") and not (
            isinstance(url, str) and urlsplit(url).scheme in ("http", "https")
        ):
            raise ConfigError(f"{where}: 'url' must be an http or https URL")
        if kind == "youtube" and not (
            isinstance(channel_id, str) and _CHANNEL_RE.match(channel_id)
        ):
            raise ConfigError(f"{where}: 'channel_id' must be UC followed by 22 characters")
        if kind in SEARCH_TYPES and (
            not isinstance(queries, list)
            or not queries
            or not all(isinstance(q, str) and q.strip() for q in queries)
        ):
            raise ConfigError(f"{where}: 'queries' must be a non-empty list of strings")

        name = entry.get("name")
        if name is not None and not (isinstance(name, str) and name.strip()):
            raise ConfigError(f"{where}: 'name' must be a non-empty string")

        include = entry.get("include") or []
        if not isinstance(include, list) or not all(isinstance(rule, str) for rule in include):
            raise ConfigError(f"{where}: 'include' must be a list of regular expressions")
        patterns = []
        for rule in include:
            try:
                patterns.append(re.compile(rule))
            except (re.error, TypeError) as e:
                raise ConfigError(f"{where}: invalid include rule {rule!r}: {e}") from None

        result.append(
            SourceConfig(
                key=key,
                type=kind,
                url=url,
                channel_id=channel_id,
                queries=tuple(q.strip() for q in queries) if kind in SEARCH_TYPES else (),
                include=tuple(patterns),
                name=name.strip() if name is not None else None,
            )
        )
    return tuple(result)


def _require(secrets: dict[str, str | None], name: str) -> str:
    value = secrets.get(name)
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def _int(
    environ: MutableMapping[str, str],
    name: str,
    default: int,
    *,
    low: int | None = None,
    high: int | None = None,
) -> int:
    raw = environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None
    if (low is not None and value < low) or (high is not None and value > high):
        raise ConfigError(f"{name} must be between {low} and {high}, got {value}")
    return value


def _bool(environ: MutableMapping[str, str], name: str, default: bool) -> bool:
    raw = environ.get(name)
    if not raw:
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _date(environ: MutableMapping[str, str], name: str, default: date) -> date:
    raw = environ.get(name)
    if not raw:
        return default
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a date like 2026-10-30, got {raw!r}") from None
