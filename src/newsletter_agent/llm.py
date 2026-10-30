"""The only way to call Claude, and the only module that imports claude_agent_sdk. Claude gets no
tools, except WebSearch in sessions that ask for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field, replace
from typing import Any, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    CLINotFoundError,
    Message,
    ProcessError,
    ResultError,
    ResultMessage,
    SystemMessage,
    query,
)

from newsletter_agent.text import storable_json

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 600
MAX_TURNS = 3
WEB_SEARCH_MAX_TURNS = 8
STRUCTURED_TOOL = "StructuredOutput"
WEB_SEARCH_TOOL = "WebSearch"
AUTH_ERRORS = frozenset({"authentication_failed", "billing_error"})
TRANSIENT_ERRORS = frozenset({"rate_limit", "server_error"})
STARTUP_TIMEOUT = "Control request timeout"


class ClaudeError(Exception):
    pass


class ClaudeAuthError(ClaudeError):
    """A person must fix something before Claude can be used again: the token, billing, unexpected
    tools, or an error that couldn't be classified.
    """

    pass


class ClaudeUnavailable(ClaudeError):
    """A transient failure (rate limit, 5xx, timeout, crash); a later run may succeed."""

    pass


class ClaudeOutputError(ClaudeError):
    """Claude answered, but without valid structured output. spent holds the usage of the
    sessions that produced nothing valid, so a caller can still count them.
    """

    def __init__(self, message: str, spent: tuple[Structured, ...] = ()) -> None:
        super().__init__(message)
        self.spent = spent


@dataclass(frozen=True)
class Structured:
    output: Any
    model: str
    tools: tuple[str, ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None


@dataclass
class _Signals:
    expected_tools: list[str]
    tools: list[str] | None = None
    status: int | None = None
    assistant_error: str | None = None
    retry_error: str | None = None


def structured(
    prompt: str,
    schema: dict[str, Any],
    model: str,
    *,
    timeout: float = TIMEOUT_SECONDS,
    web_search: bool = False,
) -> Structured:
    """Ask Claude for JSON that matches schema. Claude has no tools, or only WebSearch when
    web_search is set, and the session fails closed if Claude Code offers anything else. Raises
    ClaudeAuthError, ClaudeUnavailable or ClaudeOutputError; the Claude Code process has ended
    when it returns or raises.
    """
    return asyncio.run(_structured(prompt, schema, model, timeout, web_search))


async def _structured(
    prompt: str, schema: dict[str, Any], model: str, timeout: float, web_search: bool
) -> Structured:
    tools = [WEB_SEARCH_TOOL] if web_search else []
    options = ClaudeAgentOptions(
        tools=tools,
        allowed_tools=tools,
        mcp_servers={},
        setting_sources=[],
        permission_mode="dontAsk",
        output_format={"type": "json_schema", "schema": schema},
        model=model,
        max_turns=WEB_SEARCH_MAX_TURNS if web_search else MAX_TURNS,
    )
    signals = _Signals(expected_tools=sorted([STRUCTURED_TOOL, *tools]))
    result: Structured | None = None
    try:
        async with asyncio.timeout(timeout):
            messages = cast(AsyncGenerator[Message, None], query(prompt=prompt, options=options))
            async with contextlib.aclosing(messages) as stream:
                async for message in stream:
                    if isinstance(message, SystemMessage):
                        _on_system(message, signals)
                    elif isinstance(message, AssistantMessage) and message.error:
                        signals.assistant_error = message.error
                    elif isinstance(message, ResultMessage):
                        result = _on_result(message, model, signals)
                        break
    except TimeoutError:
        if result is not None:
            return result
        raise ClaudeUnavailable(f"no result within {timeout:.0f} s") from None
    except ResultError as e:
        data = e.data or {}
        signals.status = data.get("api_error_status") or signals.status
        raise _classify(signals, f"Claude Code returned an error: {e}") from None
    except CLINotFoundError as e:
        raise ClaudeAuthError(f"Claude Code is missing: {e}") from None
    except (CLIConnectionError, ProcessError) as e:
        raise ClaudeUnavailable(f"Claude Code failed: {type(e).__name__}: {e}") from None
    except ClaudeError:
        raise
    except Exception as e:
        if str(e).startswith(STARTUP_TIMEOUT):
            raise ClaudeUnavailable(f"Claude Code didn't start: {e}") from None
        log.exception("unclassified Claude Code error")
        raise ClaudeAuthError(f"unclassified: {type(e).__name__}: {e}") from None
    if result is None:
        raise ClaudeOutputError("the stream ended without a result")
    return result


def _on_system(message: SystemMessage, signals: _Signals) -> None:
    if message.subtype == "init":
        tools = message.data.get("tools")
        log.info(
            "claude session started",
            extra={
                "fields": {
                    "model": message.data.get("model"),
                    "api_key_source": message.data.get("apiKeySource"),
                    "tools": tools,
                }
            },
        )
        if not isinstance(tools, list) or sorted(tools) != signals.expected_tools:
            raise ClaudeAuthError(
                f"Claude Code offered tools {tools}, expected {signals.expected_tools}"
            )
        signals.tools = tools
    elif message.subtype == "api_retry":
        signals.status = message.data.get("error_status") or signals.status
        signals.retry_error = message.data.get("error") or signals.retry_error


def _on_result(message: ResultMessage, model: str, signals: _Signals) -> Structured:
    if signals.tools is None:
        raise ClaudeAuthError("Claude Code sent no init message, so its tools couldn't be checked")
    spent = Structured(
        output=None,
        model=model,
        tools=tuple(signals.tools),
        usage=dict(message.usage or {}),
        cost_usd=message.total_cost_usd,
    )
    if message.is_error:
        signals.status = message.api_error_status or signals.status
        if message.subtype.startswith("error_max") or message.terminal_reason == "max_turns":
            raise ClaudeOutputError(
                f"Claude stopped at a limit ({message.subtype}) without valid structured output",
                (spent,),
            )
        raise _classify(
            signals,
            f"Claude Code result {message.subtype}, status {message.api_error_status}, "
            f"reason {message.terminal_reason}: {(message.result or '')[:300]}",
        )
    if message.structured_output is None:
        raise ClaudeOutputError(f"no structured output: {(message.result or '')[:300]}", (spent,))
    return replace(spent, output=storable_json(message.structured_output))


def _classify(signals: _Signals, detail: str) -> ClaudeError:
    status = signals.status
    errors = {signals.assistant_error, signals.retry_error}
    if status in (401, 403) or errors & AUTH_ERRORS:
        return ClaudeAuthError(detail)
    if status == 429 or (status is not None and status >= 500) or errors & TRANSIENT_ERRORS:
        return ClaudeUnavailable(detail)
    return ClaudeAuthError(f"unclassified: {detail}")
