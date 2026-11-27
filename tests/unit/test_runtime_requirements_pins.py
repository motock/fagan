"""Tests that every runtime requirement in requirements.txt is pinned exactly.

Two installs of the same commit must resolve to the same packages, so each
runtime dependency carries an exact ``==`` pin. A floor (``>=``), a
compatible-release (``~=``) or a bare name lets an upstream release change
the installed code without a diff in the repository. These tests hold every
line of requirements.txt to that rule and check that the helper which
enforces it rejects the non-exact shapes.
"""

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_REQUIREMENTS = _REPO_ROOT / "requirements.txt"

# An exact pin: ``name==version`` with an optional extras group such as
# ``PyJWT[crypto]==2.15.1``. The version is any non-space token.
_EXACT_PIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(\[[^\]]+\])?==\S+")


def _requirement_lines(text: str) -> list[str]:
    """Return requirement lines only: blanks and comment lines dropped.

    Trailing ``# why`` comments are stripped so prose after a pin can never
    be read as part of the constraint.
    """
    lines = []
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].strip()
        if stripped:
            lines.append(stripped)
    return lines


def _is_exact_pin(requirement: str) -> bool:
    """True only when the whole requirement is ``name==version``."""
    return _EXACT_PIN_RE.fullmatch(requirement) is not None


def test_every_runtime_requirement_is_exactly_pinned() -> None:
    """Each non-comment, non-blank line of requirements.txt pins with ``==``."""
    text = _RUNTIME_REQUIREMENTS.read_text(encoding="utf-8")
    requirements = _requirement_lines(text)
    assert requirements, "requirements.txt must declare runtime requirements"
    unpinned = [line for line in requirements if not _is_exact_pin(line)]
    assert not unpinned, (
        "every runtime requirement in requirements.txt must be pinned exactly "
        f"with '=='; unpinned lines: {unpinned!r}"
    )


@pytest.mark.parametrize(
    "requirement",
    ["pkg>=1.0", "pkg~=1.0", "pkg", "pkg>1.0", "pkg==1.0 extra"],
)
def test_exact_pin_check_rejects_non_exact_requirements(requirement: str) -> None:
    """The checker rejects floors, compatible-release, bare names and stray tokens."""
    assert not _is_exact_pin(requirement), (
        f"{requirement!r} is not an exact pin and must be rejected"
    )


def test_exact_pin_check_accepts_exact_pins_with_extras() -> None:
    """The checker accepts plain and extras-qualified exact pins."""
    assert _is_exact_pin("pkg==1.0")
    assert _is_exact_pin("PyJWT[crypto]==2.15.1")
