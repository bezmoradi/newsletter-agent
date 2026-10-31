"""Fail when Claude review could change the repository instead of only reviewing it.

Claude review never edits files, commits or pushes; changes come from the owner's own Claude
Code session (CLAUDE.md, "Claude review only reviews"). The script reads the review workflow
and checks the settings that keep it so: the job's permissions, its token, the tools Claude
gets, and no commit-signing, permission-mode, plugin, settings or GitHub MCP options. Each
permission and each tool list must appear exactly once, in its one expected form, so a second
grant or another spelling of one fails the check instead of slipping past it.
Run it from the repository root: `python3 scripts/check_review_workflow.py`.
"""

import re
import sys
from collections.abc import Iterator
from pathlib import Path

WORKFLOW = Path(".github/workflows/claude-code-review.yml")
PR = "${{ github.event.pull_request.number }}"
PERMISSIONS = {"actions": "read", "contents": "read", "pull-requests": "write"}
JOB_CREDENTIAL = "github_token: ${{ github.token }}"
ALLOWED_TOOLS = {
    f"Bash(gh pr diff {PR})",
    f"Bash(gh pr diff {PR} --name-only)",
    f"Bash(gh pr view {PR})",
    "mcp__github_inline_comment__create_inline_comment",
}
DENIED_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
FORBIDDEN = re.compile(
    r"use_commit_signing|ssh_signing_key|id-token|write-all|mcp__github(?!_inline_comment__)"
    r"|permission[-_]?mode|skip[-_]?permissions|mcp[-_]?config|additional_permissions"
    r"|^\s*(?:settings|plugins|plugin_marketplaces):",
    re.IGNORECASE | re.MULTILINE,
)
PERMISSIONS_KEY = re.compile(r"^\s*permissions:(.*)$", re.MULTILINE)
PERMISSION = re.compile(r"^\s+([a-z-]+):\s*(read|write|none)\s*$")
TOOL_FLAG = re.compile(r"(?:allowed|disallowed)[-_]?tools", re.IGNORECASE)
TOOLS = re.compile(r'--(allowedTools|disallowedTools) "([^"]*)"')


def problems(text: str) -> Iterator[str]:
    values = sorted(value.strip() for value in PERMISSIONS_KEY.findall(text))
    if values != ["", "{}"]:
        yield (
            f"`permissions:` is set as {values}, not once as `{{}}` for the workflow and once as "
            "the review job's list"
        )
    granted = [
        (match[1], match[2]) for line in text.splitlines() if (match := PERMISSION.match(line))
    ]
    if len(granted) != len(dict(granted)) or dict(granted) != PERMISSIONS:
        yield f"the review job's permissions are {granted}, not exactly {PERMISSIONS} once each"
    if JOB_CREDENTIAL not in text:
        yield f"no `{JOB_CREDENTIAL}`: the action may use the Claude app's token, which can push"
    for match in FORBIDDEN.finditer(text):
        yield f"`{match.group().strip()}` would let the review do more than read and comment"
    tools = TOOLS.findall(text)
    flags = sorted(flag for flag, _ in tools)
    if len(TOOL_FLAG.findall(text)) != 2 or flags != ["allowedTools", "disallowedTools"]:
        yield (
            'the tools aren\'t set exactly once each, as `--allowedTools "..."` and '
            '`--disallowedTools "..."`'
        )
    lists = {flag: set(value.split(",")) for flag, value in tools}
    if lists.get("allowedTools") != ALLOWED_TOOLS:
        allowed = sorted(lists.get("allowedTools", set()))
        yield f"--allowedTools is {allowed}, not exactly the gh pr commands and the comment tool"
    missing = DENIED_TOOLS - lists.get("disallowedTools", set())
    if missing:
        yield f"--disallowedTools doesn't deny {sorted(missing)}"


def main() -> int:
    found = list(problems(WORKFLOW.read_text()))
    for problem in found:
        print(f"{WORKFLOW}: {problem}")
    if found:
        print(f"{len(found)} problem(s): Claude review must only review.")
        return 1
    print("Claude review only reviews.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
