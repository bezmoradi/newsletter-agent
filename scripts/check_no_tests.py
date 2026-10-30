"""Fail when the repository has tests or test tooling, which the project forbids.

Changes are verified by running the real program instead (CLAUDE.md, "Verify a change"). The
script looks for test files and folders, imports of test frameworks, doctest examples in
docstrings, test settings in pyproject.toml and test frameworks in uv.lock.
Run it from the repository root: `python3 scripts/check_no_tests.py`.
"""

import ast
import re
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

import repository

TEST_FILE = re.compile(r"(test_.*|.*_test|conftest|noxfile)\.py|pytest\.ini|tox\.ini|\.coveragerc")
TEST_FOLDERS = {"test", "tests"}
TEST_MODULES = {"unittest", "doctest", "pytest", "hypothesis", "nose", "nose2", "mock", "coverage"}
TEST_PACKAGE = re.compile(r"pytest(-.+)?|hypothesis|nose2?|mock|coverage|tox|nox")
TEST_TOOLS = {"pytest", "coverage", "tox", "nox", "hypothesis"}


def python_problems(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            for module in _imported(node):
                if module.split(".")[0] in TEST_MODULES:
                    yield f"{path}:{node.lineno}: imports {module}"
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            docstring = ast.get_docstring(node) or ""
            if any(line.lstrip().startswith(">>>") for line in docstring.splitlines()):
                line = 1 if isinstance(node, ast.Module) else node.lineno
                yield f"{path}:{line}: doctest example in a docstring"


def _imported(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    return [node.module] if node.level == 0 and node.module else []


def project_problems() -> Iterator[str]:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    for tool in sorted(TEST_TOOLS & set(pyproject.get("tool", {}))):
        yield f"pyproject.toml: [tool.{tool}] settings"
    lock = tomllib.loads(Path("uv.lock").read_text())
    for package in lock.get("package", []):
        if TEST_PACKAGE.fullmatch(package["name"]):
            yield f"uv.lock: test framework {package['name']}"


def main() -> int:
    problems = list(project_problems())
    for path in repository.files():
        if TEST_FOLDERS & set(path.parts[:-1]):
            problems.append(f"{path}: in a test folder")
        if TEST_FILE.fullmatch(path.name):
            problems.append(f"{path}: test file")
        if path.suffix == ".py":
            problems += python_problems(path)
    for problem in problems:
        print(problem)
    if problems:
        print(f"{len(problems)} problem(s): the project has no unit or integration tests.")
        return 1
    print("No tests or test tooling found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
