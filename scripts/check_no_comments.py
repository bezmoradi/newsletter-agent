"""Fail when a file in the repository has a comment, which the project forbids (REQ-24).

Python docstrings are allowed. Markdown files and the license are documents, not code or config,
and aren't checked. A kind of file the script has no check for fails it, so each new kind gets a
decision. Run it from the repository root: `python3 scripts/check_no_comments.py`.
"""

import io
import re
import sys
import tokenize
from collections.abc import Callable, Iterable

import repository

DOCUMENTS = {".md"}
DOCUMENT_NAMES = {"LICENSE"}
TOML_TOKEN = re.compile(
    r'"""(?:\\.|[^\\])*?"""|\'\'\'.*?\'\'\'|"(?:\\.|[^"\\\n])*"|\'[^\'\n]*\'|#', re.S
)
SQL_TOKEN = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--|/\*", re.S)
HASH_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|(?<!\w)\'[^\']*\'|(?<!\S)#')


def python_comments(text: str) -> list[int]:
    tokens = tokenize.generate_tokens(io.StringIO(text).readline)
    return [token.start[0] for token in tokens if token.type == tokenize.COMMENT]


def toml_comments(text: str) -> list[int]:
    return _marked(TOML_TOKEN, text, ("#",))


def sql_comments(text: str) -> list[int]:
    return _marked(SQL_TOKEN, text, ("--", "/*"))


def hash_comments(text: str) -> list[int]:
    return [
        number
        for number, line in enumerate(text.splitlines(), 1)
        if any(match.group() == "#" for match in HASH_TOKEN.finditer(line))
    ]


def systemd_comments(text: str) -> list[int]:
    semicolons = [
        number for number, line in enumerate(text.splitlines(), 1) if line.lstrip().startswith(";")
    ]
    return sorted(set(hash_comments(text)) | set(semicolons))


def _marked(pattern: re.Pattern[str], text: str, markers: Iterable[str]) -> list[int]:
    return [
        text.count("\n", 0, match.start()) + 1
        for match in pattern.finditer(text)
        if match.group() in markers
    ]


Check = Callable[[str], list[int]]
BY_NAME: dict[str, Check] = {
    "Dockerfile": hash_comments,
    ".gitignore": hash_comments,
    ".dockerignore": hash_comments,
    ".python-version": hash_comments,
}
BY_SUFFIX: dict[str, Check] = {
    ".py": python_comments,
    ".toml": toml_comments,
    ".lock": toml_comments,
    ".sql": sql_comments,
    ".yaml": hash_comments,
    ".yml": hash_comments,
    ".conf": hash_comments,
    ".service": systemd_comments,
    ".timer": systemd_comments,
}


def main() -> int:
    problems = []
    for path in repository.files():
        if path.suffix in DOCUMENTS or path.name in DOCUMENT_NAMES:
            continue
        check = BY_NAME.get(path.name) or BY_SUFFIX.get(path.suffix)
        if check is None:
            problems.append(f"{path}: no comment check for this kind of file; add one")
            continue
        problems += [f"{path}:{line}: comment" for line in check(path.read_text())]
    for problem in problems:
        print(problem)
    if problems:
        print(f"{len(problems)} problem(s): the project allows no comments (REQ-24).")
        return 1
    print("No comments found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
