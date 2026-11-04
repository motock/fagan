"""Static-text guards on deploy/install.sh for the standalone realm import permissions.

The rendered realm carries the OIDC client secrets, so ``render_realm`` must stay
private (0600) for every caller -- including ``--render-realm``, whose destination
is caller-chosen and has no private parent directory. Only the install path may
relax the published copy, and only inside ``$STATE_DIR/import``, which install.sh
keeps at 0700.
"""

from __future__ import annotations

import re
from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parents[2] / "deploy" / "install.sh"
REALM_CALL = 'render_realm "$REPO_ROOT/deploy/standalone/realm.json"'
IMPORT_DIR_CHMOD = 'chmod 700 "$STATE_DIR/import"'
RELAXATION = 'chmod 0644 "$RENDERED_REALM_FILE"'
# One `chmod <octal-mode> <target>` line; group 1 is the mode, group 2 the target.
CHMOD_RE = re.compile(r"^\s*chmod\s+([0-7]{3,4})\s+(\S.*)$")


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


def test_render_realm_never_widens_the_realm_file() -> None:
    """The shared helper must not hand group or other any access to the secrets."""
    offenders = []
    for line in _render_realm_body():
        match = CHMOD_RE.match(line)
        if match and int(match.group(1), 8) & 0o077:
            offenders.append(line.strip())
    assert offenders == [], f"render_realm() widens the rendered realm: {offenders}"


def test_render_realm_publishes_private_by_default() -> None:
    published = [
        match.group(1)
        for line in _render_realm_body()
        if (match := CHMOD_RE.match(line)) and '"$dst"' in match.group(2)
    ]
    assert published, 'render_realm() must chmod the published file ($dst) explicitly'
    assert [int(mode, 8) for mode in published] == [0o600], (
        f"render_realm() must publish at 0600 for every caller, got {published}"
    )


def test_render_realm_doc_still_promises_0600() -> None:
    assert "SRC into DST (mode 0600)" in _text(), (
        "the --render-realm contract comment must keep documenting mode 0600"
    )


def test_install_path_relaxes_only_the_published_realm_after_the_private_dir() -> None:
    text = _text()
    assert text.count(REALM_CALL) == 1, f"expected exactly one call line: {REALM_CALL!r}"
    assert text.count(IMPORT_DIR_CHMOD) == 1
    assert text.count(RELAXATION) == 1, (
        f"the install path must relax exactly one copy of the realm: {RELAXATION!r}"
    )
    dir_pos = text.index(IMPORT_DIR_CHMOD)
    call_pos = text.index(REALM_CALL)
    relax_pos = text.index(RELAXATION)
    assert dir_pos < call_pos, (
        f"chmod 700 on $STATE_DIR/import (offset {dir_pos}) must precede "
        f"the render_realm call (offset {call_pos})"
    )
    assert call_pos < relax_pos, (
        f"the 0644 relaxation (offset {relax_pos}) must follow the render_realm call "
        f"(offset {call_pos}) so it only touches the published file"
    )


def test_no_other_realm_file_is_made_world_readable() -> None:
    world_readable = [
        line.strip()
        for line in _lines()
        if (match := CHMOD_RE.match(line))
        and int(match.group(1), 8) & 0o004
        and "realm" in match.group(2).lower()
    ]
    assert world_readable == [RELAXATION], (
        f"only the install-path realm copy may be world-readable: {world_readable}"
    )
