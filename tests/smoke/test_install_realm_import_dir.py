"""Static-text guards on deploy/install.sh for the standalone realm import permissions."""

from __future__ import annotations

import re
from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parents[2] / "deploy" / "install.sh"
REALM_CALL = 'render_realm "$REPO_ROOT/deploy/standalone/realm.json"'
IMPORT_DIR_CHMOD = 'chmod 700 "$STATE_DIR/import"'
REALM_0600_RE = re.compile(r"^\s*chmod\s+0?600\b")


def _text() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


def _lines() -> list[str]:
    return _text().splitlines()


def _render_realm_body() -> list[str]:
    lines = _lines()
    starts = [i for i, line in enumerate(lines) if re.match(r"^render_realm\(\)\s*\{", line)]
    assert len(starts) == 1, f"expected exactly one render_realm() definition, found {starts}"
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() == "}"), None)
    assert end is not None, "render_realm() body is not closed by a line that is exactly '}'"
    body = lines[start : end + 1]
    assert 5 < len(body) < 120, f"render_realm() body looks mis-parsed: {len(body)} lines"
    return body


def test_import_dir_stays_private() -> None:
    assert _lines().count(IMPORT_DIR_CHMOD) == 1, "chmod 700 on $STATE_DIR/import must survive"


def test_render_realm_does_not_chmod_the_realm_file_0600() -> None:
    offenders = [line for line in _render_realm_body() if REALM_0600_RE.match(line)]
    assert offenders == [], f"render_realm() still restricts the realm file: {offenders}"


def test_import_dir_chmod_precedes_the_realm_render_call() -> None:
    text = _text()
    assert text.count(REALM_CALL) == 1, f"expected exactly one call line: {REALM_CALL!r}"
    assert text.count(IMPORT_DIR_CHMOD) == 1
    dir_pos = text.index(IMPORT_DIR_CHMOD)
    call_pos = text.index(REALM_CALL)
    assert dir_pos < call_pos, (
        f"chmod 700 on $STATE_DIR/import (offset {dir_pos}) must precede "
        f"the render_realm call (offset {call_pos})"
    )
