"""Structural wiring tests for scripts/install.sh -> scripts/install_checks.py.

This story rewires ``scripts/install.sh``: the inline ``check()`` shell
function, its per-tool invocations and the hand-rolled ollama block are
replaced by a single delegation to the pre-venv doctor module::

    "${PYTHON:-python3}" "$ROOT/scripts/install_checks.py"

House rules honoured here:

* install.sh is a SHARED artifact that later stories may extend, so these
  tests assert MEMBERSHIP and ORDERING relative to fixed anchors only --
  never the file's total contents, line count, or a hash;
* the script is never executed here (this is shell wiring, so the only
  testable surface is structural): no venv is created, no pip download
  happens, and no host tool lookup runs in-process;
* the module side is probed only through injected stubs (a ``which`` that
  finds nothing, a synthetic py_version), never the real ``shutil.which``.

RED state: until install.sh delegates, the delegation test fails because
the literal module path is absent from install.sh, the no-inline-check
test fails because ``check() {`` is still defined, and the ollama-block
test fails because the block is still inline.  That is the intended TDD
state, not a bug in this suite.
"""

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INSTALL_SH = _REPO_ROOT / "scripts" / "install.sh"
_INSTALL_CHECKS = _REPO_ROOT / "scripts" / "install_checks.py"
_MODULE_LITERAL = "scripts/install_checks.py"

# The module is a dependency from the previous story; fail loudly (not
# skip) if it vanished, so a broken dependency is never silently green.
if not _INSTALL_CHECKS.exists():
    raise ImportError(
        f"TDD dependency missing: {_INSTALL_CHECKS} does not exist. "
        "This wiring story builds on the previous story's module."
    )

_spec = importlib.util.spec_from_file_location(
    "install_checks_under_wiring_test", str(_INSTALL_CHECKS)
)
ic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ic)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _source():
    return _INSTALL_SH.read_text(encoding="utf-8")


def _code_lines(source):
    """Non-comment lines of install.sh (inline comments after code stay)."""
    return [
        ln for ln in source.splitlines() if not ln.lstrip().startswith("#")
    ]


def _invocation_lines(source):
    """Code lines that reference the install_checks module."""
    return [ln for ln in _code_lines(source) if _MODULE_LITERAL in ln]


def _line_containing(source, *needles):
    """First code line containing all needles, else None."""
    for ln in _code_lines(source):
        if all(needle in ln for needle in needles):
            return ln
    return None


# --------------------------------------------------------------------------- #
# (a) install.sh delegates to the module
# --------------------------------------------------------------------------- #
def test_install_sh_contains_module_literal():
    """(a) The literal module path appears in install.sh."""
    assert _MODULE_LITERAL in _source()


def test_install_sh_invokes_module_exactly_once():
    """Exactly one code line invokes the module (zero = not wired, >1 = dup)."""
    invocations = _invocation_lines(_source())
    assert len(invocations) == 1, (
        "expected exactly one scripts/install_checks.py invocation line, "
        f"got {invocations!r}"
    )


def test_install_sh_invocation_is_root_anchored():
    """The invocation must be $ROOT-anchored, not CWD-relative."""
    invocations = _invocation_lines(_source())
    assert invocations, "module invocation missing"
    assert "$ROOT/scripts/install_checks.py" in invocations[0]


def test_install_sh_invocation_uses_system_python_not_venv():
    """The module runs on a system python, never the (maybe absent) venv."""
    invocations = _invocation_lines(_source())
    assert invocations, "module invocation missing"
    line = invocations[0]
    assert "PYBIN" not in line, (
        f"module must not be launched via the venv interpreter: {line!r}"
    )
    system_python_markers = ('"${PYTHON:-python3}"', '"$PY"', "python3")
    assert any(marker in line for marker in system_python_markers), (
        f"module invocation must use a system python: {line!r}"
    )


# --------------------------------------------------------------------------- #
# (b) the inline check() shell function is gone
# --------------------------------------------------------------------------- #
def test_install_sh_no_longer_defines_check_function():
    """(b) The inline ``check() {`` definition is removed."""
    assert "check() {" not in _source()


def test_install_sh_no_longer_invokes_check():
    """No leftover ``check <tool>`` invocation lines survive."""
    code = "\n".join(_code_lines(_source()))
    assert re.search(r"(?m)^\s*check\s+\S", code) is None, (
        "install.sh must not invoke the removed inline check() helper"
    )


def test_install_sh_ollama_block_replaced():
    """The hand-rolled ollama block (model pull hint machinery) is gone."""
    code = "\n".join(_code_lines(_source()))
    for gone in (
        "PIPELINE_LOCAL_MODEL_DEFAULT",
        "ollama list",
        "ollama pull",
        "command -v ollama",
    ):
        assert gone not in code, f"stale ollama-block line survived: {gone!r}"


# --------------------------------------------------------------------------- #
# (c) the module lists docker among its OPTIONAL checks
# --------------------------------------------------------------------------- #
def _collect_with_nothing_present():
    """collect_checks on a synthetic host where no tool exists."""
    return ic.collect_checks(which=lambda name: None, py_version=(3, 11))


def test_install_checks_lists_docker_among_optional_checks():
    """(c) docker is in the inventory exactly once, flagged optional."""
    checks = _collect_with_nothing_present()
    docker = [c for c in checks if c["name"] == "docker"]
    assert len(docker) == 1, (
        f"expected exactly one docker check, got {docker!r}"
    )
    assert docker[0]["required"] is False
    assert docker[0]["status"] == "missing"


def test_install_checks_docker_message_says_optional():
    """Graceful degradation: the docker hint says it is optional."""
    checks = _collect_with_nothing_present()
    hint = next(c["hint"] for c in checks if c["name"] == "docker")
    assert "optional" in hint.lower()


def test_install_checks_ollama_still_optional():
    """The module's ollama coverage survives alongside docker (membership)."""
    optional_names = {
        c["name"] for c in _collect_with_nothing_present() if not c["required"]
    }
    assert {"ollama", "docker"} <= optional_names


def test_module_main_returns_zero_when_checks_missing(monkeypatch):
    """install.sh runs under `set -e`, so the module must always exit 0."""
    monkeypatch.setattr(ic, "collect_checks", lambda *a, **k: [])
    assert ic.main([]) == 0


# --------------------------------------------------------------------------- #
# deletion survivors: variable resolution / guards / venv / pip / done block
# --------------------------------------------------------------------------- #
def test_survivor_set_euo_pipefail():
    assert "set -euo pipefail" in _source()


def test_survivor_variable_resolution():
    src = _source()
    for literal in (
        'ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',
        'VENV="$ROOT/.venv"',
        'PYBIN="$VENV/bin/python3"',
        'REQ="requirements.txt"',
    ):
        assert literal in src, f"survivor variable line missing: {literal!r}"


def test_survivor_dev_requirements_switch():
    src = _source()
    assert '[ "${1:-}" = "--dev" ]' in src
    assert 'REQ="requirements-dev.txt"' in src


def test_survivor_python3_presence_check():
    assert _line_containing(_source(), 'command -v "$PY"') is not None, (
        "python3 presence check must survive"
    )


def test_survivor_python_version_guard():
    assert re.search(
        r"sys\.version_info\[:2\]\s*>=\s*\(\s*3\s*,\s*10\s*\)", _source()
    ), "Python 3.10+ version guard must survive"


def test_survivor_venv_create_if_missing():
    src = _source()
    guard = _line_containing(src, '[ ! -x "$PYBIN" ]')
    create = _line_containing(src, '-m venv "$VENV"')
    assert guard is not None, "venv create-if-missing guard must survive"
    assert create is not None, "venv creation must survive"
    code = _code_lines(src)
    assert code.index(guard) < code.index(create), (
        "venv creation must stay guarded by the create-if-missing check"
    )


def test_survivor_pip_upgrade_invocation():
    assert _line_containing(_source(), "-m pip install", "--upgrade pip") is not None


def test_survivor_pip_install_requirements_invocation():
    line = _line_containing(_source(), "-m pip install", '-r "$ROOT/$REQ"')
    assert line is not None, "pip install -r requirements invocation must survive"


def test_survivor_pip_list_confirmation():
    assert _line_containing(_source(), "-m pip list") is not None


def test_survivor_done_block():
    assert "==> Done. Next steps:" in _source()


# --------------------------------------------------------------------------- #
# ordering: the early guards must run before the module can be launched
# --------------------------------------------------------------------------- #
def test_guards_precede_module_invocation():
    src = _source()
    code = _code_lines(src)
    invocations = _invocation_lines(src)
    assert invocations, "module invocation missing"
    first_invocation = code.index(invocations[0])

    presence = _line_containing(src, 'command -v "$PY"')
    assert presence is not None, "python3 presence check missing"
    version = _line_containing(src, "sys.version_info")
    assert version is not None, "python version guard missing"

    assert code.index(presence) < first_invocation, (
        "python3 presence check must run before the module is launched"
    )
    assert code.index(version) < first_invocation, (
        "Python 3.10+ version guard must run before the module is launched"
    )


# --------------------------------------------------------------------------- #
# shell syntax
# --------------------------------------------------------------------------- #
def test_install_sh_passes_bash_syntax_check():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available on this host")
    proc = subprocess.run(
        [bash, "-n", str(_INSTALL_SH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"bash -n failed:\n{proc.stderr}"