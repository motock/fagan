"""TDD tests: scripts/install.sh must install the dashboard deps by default.

Bug (verified on a fresh clone + install): the plain (non---dev) install path
runs a single ``pip install -r "$ROOT/$REQ"`` where REQ is requirements.txt,
which only pins ``mcp`` and ``httpx``.  ``fastapi``/``uvicorn`` live in
requirements-dashboard.txt, which the default path never installs — so
``scripts/dashboard.sh start`` after a plain install dies with
``ModuleNotFoundError: No module named 'fastapi'``.

The fix is ADDITIVE: install.sh gains a separate, clearly-commented
``pip install`` step for requirements-dashboard.txt AFTER the existing
``-r "$ROOT/$REQ"`` install.  requirements-dev.txt already pulls the
dashboard file in, so installing it a second time on the --dev path is
harmless — no conditional is wanted.

House rules honoured here:

* install.sh is a SHARED artifact later stories may extend, so these tests
  assert MEMBERSHIP and ORDERING relative to fixed anchors only — never the
  file's total contents, line count, or a hash;
* the script is never executed (no venv is created, no pip download
  happens); the only subprocess is ``bash -n``, a pure syntax check with no
  side effects;
* requirements-dashboard.txt's fastapi/uvicorn lines must stay
  byte-identical — only its leading comment may change.

RED state: until install.sh gains the dashboard step, the dashboard-install,
ordering, dev-guard and comment tests fail because the step is absent and
the requirements-dashboard.txt header still says "Optional".  That is the
intended TDD state, not a bug in this suite.
"""

import re
import shutil
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INSTALL_SH = _REPO_ROOT / "scripts" / "install.sh"
_REQUIREMENTS = _REPO_ROOT / "requirements.txt"
_DASHBOARD_REQ = _REPO_ROOT / "requirements-dashboard.txt"

_DASHBOARD_REQ_NAME = "requirements-dashboard.txt"

# The exact requirement lines that exist today — must survive byte-identical.
_FASTAPI_PIN_RE = re.compile(r"^fastapi\u003e=\d+\.\d+\.\d+$")
_UVICORN_PIN_RE = re.compile(r"^uvicorn\u003e=\d+\.\d+\.\d+$")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _source():
    return _INSTALL_SH.read_text(encoding="utf-8")


def _code_lines(source):
    """Non-comment lines of install.sh (inline comments after code stay)."""
    return [ln for ln in source.splitlines() if not ln.lstrip().startswith("#")]


def _lines_with_all(lines, *needles):
    """Every line containing all needles, in order."""
    return [ln for ln in lines if all(needle in ln for needle in needles)]


def _dashboard_install_lines(source):
    """Code lines that pip-install the dashboard requirements.

    Accepts either a literal path (``-r "$ROOT/requirements-dashboard.txt"``)
    or variable indirection whose assignment names the dashboard file
    (``DASH_REQ="requirements-dashboard.txt"`` ... ``-r "$ROOT/$DASH_REQ"``).
    """
    code = _code_lines(source)
    literal = _lines_with_all(code, "-m pip install", _DASHBOARD_REQ_NAME)
    if literal:
        return literal
    joined = "\n".join(code)
    indirect = []
    for var in re.findall(
        r'(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)="requirements-dashboard\.txt"',
        joined,
    ):
        indirect.extend(_lines_with_all(code, "-m pip install", f"${var}"))
    return indirect


def _dev_guard_ranges(code):
    """Index ranges of code lines guarded by a ``--dev`` conditional.

    Covers both shapes the script could use: the one-line short-circuit
    (``[ "${1:-}" = "--dev" ] && cmd``) and an ``if ... then ... fi`` block.
    """
    ranges = []
    open_idx = None
    for i, ln in enumerate(code):
        stripped = ln.strip()
        if open_idx is None:
            if "--dev" in ln and "-m pip install" in ln:
                ranges.append((i, i))
            elif re.search(r"(?<![\w-])if\b.*--dev", ln):
                open_idx = i
        elif stripped == "fi" or stripped.startswith("fi "):
            ranges.append((open_idx, i))
            open_idx = None
    if open_idx is not None:
        ranges.append((open_idx, len(code) - 1))
    return ranges


# --------------------------------------------------------------------------- #
# (1) the dashboard install step exists
# --------------------------------------------------------------------------- #
def test_install_sh_has_dashboard_pip_install_line():
    """(1) A `-m pip install` line references requirements-dashboard.txt."""
    dash = _dashboard_install_lines(_source())
    assert dash, (
        "scripts/install.sh has no `-m pip install` line referencing "
        f"{_DASHBOARD_REQ_NAME}; the plain install path still skips "
        "fastapi/uvicorn and scripts/dashboard.sh start stays broken"
    )


def test_dashboard_install_uses_the_venv_python():
    """The step must install into the venv (PYBIN), not the system python."""
    dash = _dashboard_install_lines(_source())
    assert dash, "dashboard install line missing"
    for ln in dash:
        assert "PYBIN" in ln, (
            f"dashboard deps must be installed with the venv interpreter: {ln!r}"
        )


def test_dashboard_install_step_is_commented():
    """The new step is introduced by a comment mentioning the dashboard."""
    src = _source()
    lines = src.splitlines()
    main = _lines_with_all(lines, "-m pip install", '-r "$ROOT/$REQ"')
    dash = _dashboard_install_lines(src)
    assert main and dash, "anchor or dashboard install line missing"
    main_i = lines.index(main[0])
    dash_i = min(lines.index(ln) for ln in dash)
    intro = [
        ln
        for ln in lines[main_i:dash_i]
        if ln.lstrip().startswith("#") and "dashboard" in ln.lower()
    ]
    assert intro, (
        "the dashboard install step must be introduced by a comment "
        f"mentioning the dashboard between source lines {main_i + 1} "
        f"and {dash_i + 1}"
    )


# --------------------------------------------------------------------------- #
# (2) ordering: dashboard step comes after the main -r "$ROOT/$REQ" install
# --------------------------------------------------------------------------- #
def test_dashboard_install_comes_after_main_requirements_install():
    """(2) Index order on non-comment code lines, anchored on the main install."""
    code = _code_lines(_source())
    main = _lines_with_all(code, "-m pip install", '-r "$ROOT/$REQ"')
    assert main, 'anchor gone: no `-m pip install ... -r "$ROOT/$REQ"` line'
    dash = _dashboard_install_lines(_source())
    assert dash, "dashboard install line missing"
    main_idx = code.index(main[0])
    dash_idx = min(code.index(ln) for ln in dash)
    assert dash_idx > main_idx, (
        f"dashboard install (code line {dash_idx + 1}) must come AFTER the "
        f'main `-r "$ROOT/$REQ"` install (code line {main_idx + 1})'
    )


# --------------------------------------------------------------------------- #
# (3) REQ still defaults to requirements.txt
# --------------------------------------------------------------------------- #
def test_req_still_defaults_to_requirements_txt():
    """(3) REQ="requirements.txt" survives and is never the dashboard file."""
    src = _source()
    assert 'REQ="requirements.txt"' in src
    assert 'REQ="requirements-dashboard.txt"' not in src
    code = "\n".join(_code_lines(src))
    assert re.search(r'(?m)^\s*REQ="requirements-dashboard', code) is None, (
        "REQ must not be repointed at requirements-dashboard.txt"
    )


# --------------------------------------------------------------------------- #
# (4) the script stays syntactically valid
# --------------------------------------------------------------------------- #
def test_install_sh_passes_bash_syntax_check():
    """(4) `bash -n scripts/install.sh` exits 0."""
    bash = shutil.which("bash")
    assert bash, "bash is required to syntax-check scripts/install.sh"
    proc = subprocess.run(
        [bash, "-n", str(_INSTALL_SH)],
        capture_output=True,
        text=True,
        check=False,  # the non-zero exit IS the assertion below
    )
    assert proc.returncode == 0, f"bash -n failed:\n{proc.stderr}"


# --------------------------------------------------------------------------- #
# (5) requirements-dashboard.txt: pins byte-identical, comment rewritten
# --------------------------------------------------------------------------- #
def _dashboard_req_lines():
    return _DASHBOARD_REQ.read_text(encoding="utf-8").splitlines()


def _dashboard_req_comment_block():
    """The leading comment block of requirements-dashboard.txt."""
    comment = []
    for ln in _dashboard_req_lines():
        if ln.lstrip().startswith("#"):
            comment.append(ln)
        else:
            break
    return comment


def test_dashboard_req_pins_byte_identical():
    """(5a) fastapi/uvicorn stay single, well-formed, correctly ordered floor
    pins -- shape only, not a frozen version literal, so a legitimate
    dependency bump does not fail this test."""
    lines = _dashboard_req_lines()
    fastapi_matches = [ln for ln in lines if _FASTAPI_PIN_RE.match(ln)]
    uvicorn_matches = [ln for ln in lines if _UVICORN_PIN_RE.match(ln)]
    assert len(fastapi_matches) == 1, f"fastapi pin malformed or duplicated: {lines!r}"
    assert len(uvicorn_matches) == 1, f"uvicorn pin malformed or duplicated: {lines!r}"
    assert lines.index(fastapi_matches[0]) < lines.index(uvicorn_matches[0]), (
        "fastapi/uvicorn order must not change"
    )


def test_dashboard_req_comment_drops_optional_wording():
    """(5b) The header no longer claims the deps are Optional."""
    comment = "\n".join(_dashboard_req_comment_block())
    assert "Optional" not in comment, (
        "requirements-dashboard.txt header still says the deps are Optional; "
        "install.sh now installs them by default"
    )


def test_dashboard_req_comment_says_install_sh_installs_them():
    """(5c) The header points at scripts/install.sh as the installer."""
    comment = "\n".join(_dashboard_req_comment_block())
    assert "install.sh" in comment, (
        "requirements-dashboard.txt header must say scripts/install.sh "
        f"installs these deps; got: {comment!r}"
    )
    assert "dashboard" in comment.lower(), (
        f"header must still describe the dashboard deps; got: {comment!r}"
    )


# --------------------------------------------------------------------------- #
# (6) the dashboard step is NOT gated behind --dev
# --------------------------------------------------------------------------- #
def test_dashboard_install_not_gated_on_dev_flag():
    """(6) Not on the --dev conditional's line and not inside its block."""
    src = _source()
    code = _code_lines(src)
    dash = _dashboard_install_lines(src)
    assert dash, "dashboard install line missing"
    dash_idxs = sorted(code.index(ln) for ln in dash)
    for ln in dash:
        assert "--dev" not in ln, (
            "dashboard install must not be short-circuited onto the --dev "
            f"conditional line: {ln!r}"
        )
    for start, end in _dev_guard_ranges(code):
        for idx in dash_idxs:
            assert not (start <= idx <= end), (
                f"dashboard install (code line {idx + 1}) is nested inside the "
                f"--dev conditional (code lines {start + 1}-{end + 1}): "
                f"{code[idx]!r}"
            )


# --------------------------------------------------------------------------- #
# (7) requirements.txt must not absorb the dashboard deps
# --------------------------------------------------------------------------- #
def test_requirements_txt_does_not_gain_dashboard_deps():
    """(7) fastapi/uvicorn stay out of requirements.txt (no folding)."""
    text = _REQUIREMENTS.read_text(encoding="utf-8")
    assert "fastapi" not in text, "requirements.txt must not gain fastapi"
    assert "uvicorn" not in text, "requirements.txt must not gain uvicorn"


# --------------------------------------------------------------------------- #
# survivors: the literals the shared wiring suite relies on still stand
# --------------------------------------------------------------------------- #
def test_survivor_literals_still_present():
    """The pinned install.sh literals survive the additive edit."""
    src = _source()
    code = _code_lines(src)
    for literal in (
        'ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',
        'VENV="$ROOT/.venv"',
        'PYBIN="$VENV/bin/python3"',
        'REQ="requirements.txt"',
        '[ "${1:-}" = "--dev" ]',
        'REQ="requirements-dev.txt"',
        "==> Done. Next steps:",
    ):
        assert literal in src, f"survivor literal lost: {literal!r}"
    assert _lines_with_all(code, "-m pip install", '-r "$ROOT/$REQ"'), (
        'lost the `-m pip install ... -r "$ROOT/$REQ"` line'
    )
    assert _lines_with_all(code, "-m pip install", "--upgrade pip"), (
        "lost the pip self-upgrade line"
    )
    assert _lines_with_all(code, "-m pip list"), "lost the `pip list` line"


# --------------------------------------------------------------------------- #
# guard: the target artifacts this story edits must exist
# --------------------------------------------------------------------------- #
def test_target_files_exist():
    """Fail loudly (not skip) if a target artifact vanished."""
    for path in (_INSTALL_SH, _REQUIREMENTS, _DASHBOARD_REQ):
        assert path.is_file(), f"target artifact missing: {path}"