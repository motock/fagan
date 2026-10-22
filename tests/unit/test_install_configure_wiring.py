"""Tests for the install.sh -> scripts/configure.py wiring."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parents[2] / "scripts" / "install.sh"
TEXT = INSTALL_SH.read_text()
LINES = TEXT.splitlines()


def _configure_invocation_lines():
    return [ln for ln in LINES
            if '"$PYBIN" "$ROOT/scripts/configure.py"' in ln
            and "re-run" not in ln]


def test_should_invoke_configure_exactly_once():
    assert len(_configure_invocation_lines()) == 1


def test_should_invoke_configure_with_pybin_anchored_at_root():
    line = _configure_invocation_lines()[0]

    assert '"$PYBIN" "$ROOT/scripts/configure.py"' in line


def test_should_not_hardcode_repo_root_flag():
    assert "--repo-root" not in TEXT


def test_should_invoke_configure_before_done_header():
    assert TEXT.index('"$ROOT/scripts/configure.py" $CONFIGURE_ARGS') < TEXT.index(
        "==> Done. Next steps:")


def test_should_have_no_read_prompt():
    assert not [ln for ln in LINES if re.match(r"\s*read\b", ln)]


def test_should_not_invoke_choose_providers():
    code = [ln for ln in LINES if "choose_providers.py" in ln]
    assert all(ln.lstrip().startswith("echo") for ln in code)


def test_should_run_install_checks_on_exactly_one_line():
    assert len([ln for ln in LINES if "scripts/install_checks.py" in ln
                and not ln.lstrip().startswith("#")]) == 1


def test_should_disable_sc2086_above_the_invocation():
    idx = TEXT.index("# shellcheck disable=SC2086")
    assert idx < TEXT.index('$CONFIGURE_ARGS; then')


def test_should_pass_bash_syntax_check():
    assert subprocess.run(["bash", "-n", str(INSTALL_SH)], check=False).returncode == 0


def _flag_loop_snippet():
    start = TEXT.index("CONFIGURE_ARGS=")
    end = TEXT.index('echo "==> Pipeline install')
    return TEXT[start:end]


def _run_flag_loop(args):
    script = (
        "set -euo pipefail\nREQ=requirements.txt\n"
        + _flag_loop_snippet()
        + '\nprintf "%s|%s" "$REQ" "$CONFIGURE_ARGS"\n'
    )
    out = subprocess.run(["bash", "-c", script, "x", *args],
                         capture_output=True, text=True, check=True)
    return out.stdout


def test_should_leave_configure_args_empty_without_args():
    assert _run_flag_loop([]) == "requirements.txt|"


def test_should_set_dev_requirements_for_dev_flag():
    assert _run_flag_loop(["--dev"]) == "requirements-dev.txt|"


def test_should_set_dev_requirements_for_dev_in_any_position():
    assert _run_flag_loop(["--skip-services", "--dev"]) == (
        "requirements-dev.txt|--skip-services")


def test_should_forward_operator_flags_verbatim():
    assert _run_flag_loop(["--repo-root", "/tmp/project"]) == (
        "requirements.txt|--repo-root /tmp/project")
