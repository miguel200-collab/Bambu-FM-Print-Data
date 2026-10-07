"""
Static guards. Two of the bugs that blocked the pipeline in July were plain
undefined names (`log` in main.py, `_Timeout` in printer_uploader.py) that only
blew up at runtime, deep inside a thread. pyflakes finds that class of bug
without running anything.
"""

import ast
from pathlib import Path

import pytest
from pyflakes.checker import Checker
from pyflakes import messages

REPO = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", "node_modules", "web", ".git", "__pycache__", ".pytest_cache"}


def project_files():
    return sorted(
        p for p in REPO.rglob("*.py")
        if not (set(p.relative_to(REPO).parts) & SKIP_DIRS)
    )


def test_project_files_are_found():
    names = {p.name for p in project_files()}
    assert {"main.py", "database.py", "mqtt_listener.py", "file_watcher.py", "api_server.py"} <= names


@pytest.mark.parametrize("path", project_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_no_undefined_names_and_files_compile(path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))            # SyntaxError fails the test
    checker = Checker(tree, filename=str(path))
    fatal = (messages.UndefinedName, messages.UndefinedLocal, messages.UndefinedExport,
             messages.ImportStarUsed, messages.DuplicateArgument)
    problems = [m.message % m.message_args for m in checker.messages if isinstance(m, fatal)]
    assert problems == [], f"{path.name}: {problems}"


def test_the_checker_actually_catches_the_july_bugs():
    """Guard the guard: both historical bugs must be detected by this approach."""
    for snippet in ("def f():\n    log.info('x')\n", "TIMEOUT = 1\ndef g():\n    return _Timeout\n"):
        checker = Checker(ast.parse(snippet), filename="snippet.py")
        assert any(isinstance(m, messages.UndefinedName) for m in checker.messages)
