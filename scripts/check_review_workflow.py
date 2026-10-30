"""Fail when Claude review could change the repository instead of only reviewing it.

Claude review never edits files, commits or pushes; changes come from the owner's own Claude
Code session (CLAUDE.md, "Claude review only reviews"). The script reads the review workflow
and checks the settings that keep it so: the job's permissions, its token, the tools Claude
gets, and no commit-signing or GitHub MCP tools.
Run it from the repository root: `python3 scripts/check_review_workflow.py`.
"""

import re
import sys
from collections.abc import Iterator
from pathlib import Path

WORKFLOW = Path(".github/workflows/claude-code-review.yml")
PR = "${{ github.event.pull_request.number }}"
PERMISSIONS = {"contents": "read", "pull-requests": "write"}
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
)
PERMISSION = re.compile(r"^\s+([a-z-]+):\s*(read|write|none)\s*$")
TOOLS = re.compile(r'--(allowedTools|disallowedTools)\s+"([^"]*)"')


def problems(text: str) -> Iterator[str]:
    granted = {
        match[1]: match[2] for line in text.splitlines() if (match := PERMISSION.match(line))
    }
    if granted != PERMISSIONS:
        yield f"the review job's permissions are {granted}, not exactly {PERMISSIONS}"
    if JOB_CREDENTIAL not in text:
        yield f"no `{JOB_CREDENTIAL}`: the action may use the Claude app's token, which can push"
    for match in FORBIDDEN.finditer(text):
        yield f"`{match.group()}` would let the review write to the repository"
    tools = {flag: set(value.split(",")) for flag, value in TOOLS.findall(text)}
    if tools.get("allowedTools") != ALLOWED_TOOLS:
        allowed = sorted(tools.get("allowedTools", set()))
        yield f"--allowedTools is {allowed}, not exactly the gh pr commands and the comment tool"
    missing = DENIED_TOOLS - tools.get("disallowedTools", set())
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
