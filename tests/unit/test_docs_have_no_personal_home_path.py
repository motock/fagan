"""Guard: archived plans, specs and retros must not leak a personal home path.

HK-4 scrubbed the maintainer's home directory out of the archived docs. Plan
JSON keeps a real absolute path on its ``"repo_root"`` line because re-ingest
needs one, so those lines are exempt. Every other line must use ``~`` (or the
``-Users-<you>-`` slug form inside a ``~/.claude/projects`` path).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("docs", "retros")
SCAN_SUFFIXES = (".md", ".json")
REPO_ROOT_KEY = '"repo_root"'

# Built by concatenation so this guard file never contains the literal it bans.
LITERAL = "/Users/" + "jesse" + "carroll"
PERSONAL_HOME_PREFIXES = ("/Users/", "/home/")


def _forbidden_tokens(home: str) -> tuple[str, ...]:
    """Tokens a scanned line must not contain.

    The machine's own home is banned only when it is a personal home. A service
    account home such as /var/lib/pipeline is the documented state volume, so
    the deploy docs legitimately name it.
    """
    is_personal = any(
        home.startswith(prefix) and len(home) > len(prefix)
        for prefix in PERSONAL_HOME_PREFIXES
    )
    return (home, LITERAL) if is_personal else (LITERAL,)


FORBIDDEN = _forbidden_tokens(str(Path.home()))


def _iter_scanned_files() -> list[Path]:
    files: list[Path] = []
    for dirname in SCAN_DIRS:
        base = REPO_ROOT / dirname
        for suffix in SCAN_SUFFIXES:
            files.extend(sorted(base.rglob(f"*{suffix}")))
    return files


def _offenders() -> list[str]:
    found: list[str] = []
    for path in _iter_scanned_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if REPO_ROOT_KEY in line:
                continue
            if any(token in line for token in FORBIDDEN):
                rel = path.relative_to(REPO_ROOT)
                found.append(f"{rel}:{lineno}: {line.strip()}")
    return found


def test_no_personal_home_path_in_archived_docs() -> None:
    offenders = _offenders()
    assert offenders == [], (
        "personal home path leaked into archived docs (use ~ instead):\n"
        + "\n".join(offenders)
    )


def test_should_forbid_a_macos_personal_home() -> None:
    assert "/Users/someone" in _forbidden_tokens("/Users/someone")


def test_should_forbid_a_linux_personal_home() -> None:
    assert "/home/someone" in _forbidden_tokens("/home/someone")


def test_should_not_forbid_a_service_account_home() -> None:
    assert "/var/lib/pipeline" not in _forbidden_tokens("/var/lib/pipeline")


def test_should_not_forbid_a_bare_home_root() -> None:
    assert "/home/" not in _forbidden_tokens("/home/")
    assert "/" not in _forbidden_tokens("/")


def test_should_always_forbid_the_maintainer_literal() -> None:
    assert LITERAL in _forbidden_tokens("/var/lib/pipeline")


def _plan_json_files() -> list[Path]:
    return sorted((REPO_ROOT / "docs" / "plans").glob("*.json"))


@pytest.mark.parametrize("plan_path", _plan_json_files(), ids=lambda p: p.name)
def test_plan_json_still_parses(plan_path: Path) -> None:
    json.loads(plan_path.read_text(encoding="utf-8"))
