"""Static-text guards on the standalone realm import permissions.

Keycloak imports ``realm.json`` as uid 1000 inside the container and bind-mounts it
read-only, so the published copy must be readable by that uid even when the
installing operator is a different uid (root under sudo, any non-1000 account).
The file also carries the two OIDC client secrets, so readability is granted by
mode 0644 while every caller renders into a directory kept at 0700. These tests
pin both halves of that trade-off so neither can regress silently:

* ``deploy/install.sh`` renders into ``$STATE_DIR/import`` (0700) and publishes the
  realm at 0644;
* ``deploy/standalone/.env.example`` documents the manual compose path, which must
  create its own 0700 directory and keep the rendered file inside it.

The chmod matching deliberately accepts leading flags (``-R``) and symbolic modes
(``o+r``, ``a+r``) so a widening chmod cannot evade the audit by spelling.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"
ENV_EXAMPLE = REPO_ROOT / "deploy" / "standalone" / ".env.example"

REALM_CALL = 'render_realm "$REPO_ROOT/deploy/standalone/realm.json"'
IMPORT_DIR = "$STATE_DIR/import"
IMPORT_DIR_CHMOD = f'chmod 700 "{IMPORT_DIR}"'
IMPORT_DIR_MKDIR = f'mkdir -m 0700 -p -- "{IMPORT_DIR}"'

RECIPE_DIR = "#   mkdir -p -m 0700 deploy/standalone/import"
RECIPE_RENDER = (
    "#   bash deploy/install.sh --render-realm "
    "deploy/standalone/realm.json deploy/standalone/import/realm.json"
)

# `chmod [-flags] <octal-mode> <target>`; group 1 is the mode, group 2 the target.
OCTAL_CHMOD_RE = re.compile(r"^\s*chmod\s+(?:-[A-Za-z-]+\s+)*([0-7]{3,4})\s+(\S.*)$")
# `chmod [-flags] <symbolic-mode> <target>`, e.g. ugo+r, o=r, a+rwX.
SYMBOLIC_CHMOD_RE = re.compile(r"^\s*chmod\s+(?:-[A-Za-z-]+\s+)*([ugoa]*[+=-][rwxXst]*)\s+(\S.*)$")


def _text() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


def _lines() -> list[str]:
    return _text().splitlines()


def _env_text() -> str:
    return ENV_EXAMPLE.read_text(encoding="utf-8")


def _render_realm_body() -> list[str]:
    lines = _lines()
    starts = [i for i, line in enumerate(lines) if re.match(r"^render_realm\(\)\s*\{", line)]
    assert len(starts) == 1, f"expected exactly one render_realm() definition, found {starts}"
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() == "}"), None)
    assert end is not None, "render_realm() body is not closed by a line that is exactly '}'"
    body = lines[start : end + 1]
    # Guard against truncating the region at an inner `}`: the body must span both
    # the temp-file and the published-file chmod, or the audit below is blind.
    assert 5 < len(body) < 120, f"render_realm() body looks mis-parsed: {len(body)} lines"
    return body


def test_import_dir_stays_private() -> None:
    assert _lines().count(IMPORT_DIR_CHMOD) == 1, f"chmod 700 on {IMPORT_DIR} must survive"


def test_import_dir_is_created_at_0700_without_a_chmod_window() -> None:
    assert _lines().count(IMPORT_DIR_MKDIR) == 1, (
        f"create the import directory private in one step: {IMPORT_DIR_MKDIR!r}"
    )
    text = _text()
    assert text.index(IMPORT_DIR_MKDIR) < text.index(IMPORT_DIR_CHMOD), (
        "mkdir -m 0700 must precede the chmod normalisation"
    )


def test_import_dir_must_not_be_a_symlink() -> None:
    assert re.search(r'\[\s*-L\s+"\$STATE_DIR/import"\s*\]', _text()), (
        "refuse a symlinked import directory so the chmod cannot be redirected"
    )


def test_render_realm_publishes_the_realm_readable_by_the_keycloak_user() -> None:
    published = [
        match.group(1)
        for line in _render_realm_body()
        if (match := OCTAL_CHMOD_RE.match(line)) and '"$dst"' in match.group(2)
    ]
    assert published, 'render_realm() must chmod the published file ($dst) explicitly'
    assert [int(mode, 8) for mode in published] == [0o644], (
        f"the published realm must be 0644 for uid 1000, got {published}"
    )


def test_render_realm_never_restricts_the_realm_to_0600() -> None:
    restricted = []
    for line in _render_realm_body():
        match = OCTAL_CHMOD_RE.match(line)
        if match and not int(match.group(1), 8) & 0o044:
            restricted.append(line.strip())
    assert restricted == [], (
        f"render_realm() makes the realm unreadable by uid 1000: {restricted}"
    )


def test_no_chmod_widens_a_realm_file_via_flags_or_symbolic_modes() -> None:
    offenders = []
    for line in _lines():
        stripped = line.lstrip()
        if "realm" not in line.lower() or not stripped.startswith("chmod"):
            continue
        if SYMBOLIC_CHMOD_RE.match(line) or re.search(r"\s-[A-Za-z]*[Rr]", line):
            offenders.append(line.strip())
            continue
        match = OCTAL_CHMOD_RE.match(line)
        if match and match.group(1) != "0644" and int(match.group(1), 8) & 0o077:
            offenders.append(line.strip())
    assert offenders == [], f"a chmod widens a realm file beyond 0644: {offenders}"


def test_import_dir_chmod_precedes_the_realm_render_call() -> None:
    text = _text()
    assert text.count(REALM_CALL) == 1, f"expected exactly one call line: {REALM_CALL!r}"
    assert text.count(IMPORT_DIR_CHMOD) == 1
    dir_pos = text.index(IMPORT_DIR_CHMOD)
    call_pos = text.index(REALM_CALL)
    assert dir_pos < call_pos, (
        f"chmod 700 on {IMPORT_DIR} (offset {dir_pos}) must precede "
        f"the render_realm call (offset {call_pos})"
    )


def test_env_example_recipe_renders_into_a_private_directory() -> None:
    text = _env_text()
    assert text.count(RECIPE_RENDER) == 1, (
        f"the documented manual recipe must appear exactly once: {RECIPE_RENDER!r}"
    )
    assert text.count(RECIPE_DIR) == 1, (
        f"the recipe must create the 0700 directory first: {RECIPE_DIR!r}"
    )
    assert text.index(RECIPE_DIR) < text.index(RECIPE_RENDER)
    assert "Never copy import/realm.json out of that 0700 directory" in text, (
        "the recipe must warn that the 0700 directory is what keeps the secrets private"
    )
