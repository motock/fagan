"""Regression guard for ``XDG_CONFIG_HOME`` forwarding through the installer.

``scripts/install_global_rules.py`` forwards the live environment to the
install engine. It must forward ``XDG_CONFIG_HOME`` too: opencode reads
``$XDG_CONFIG_HOME/opencode/AGENTS.md`` whenever ``OPENCODE_CONFIG_DIR`` is
unset, so dropping the variable wrote the bundle to a path opencode never
reads -- and reported success while doing it.

The third test guards the sibling CLI suite's hermeticity. CI sets
``XDG_CONFIG_HOME``; a test whose expectation is derived from ``HOME`` alone
passes on a developer machine and fails there.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from scripts import install_global_rules as cli

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_SUITE = "tests/unit/test_install_global_rules_cli.py"

_OPENCODE_LINE_RE = re.compile(
    r"^opencode: (.+) \((created|updated|unchanged|would-create|would-update)\)$"
)


def _make_source(root: Path) -> Path:
    (root / "global-rules").mkdir(parents=True, exist_ok=True)
    (root / "global-rules" / "standards.md").write_text("STANDARDS-BODY\n", encoding="utf-8")
    (root / "global-rules" / "pipeline-workflow.md").write_text(
        "WORKFLOW-BODY\n", encoding="utf-8"
    )
    rules = root / ".claude" / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "code-review.md").write_text("RULES-BODY\n", encoding="utf-8")
    return root


def _opencode_target(out: str) -> tuple[Path, str]:
    lines = [line for line in out.splitlines() if line.startswith("opencode: ")]
    assert len(lines) == 1, out
    match = _OPENCODE_LINE_RE.match(lines[0])
    assert match, lines[0]
    return Path(match.group(1)), match.group(2)


# --- the behavior the fix delivers ------------------------------------------

def test_opencode_target_follows_xdg_config_home_when_set(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    src = _make_source(tmp_path / "src")

    assert cli.main(["--tools", "opencode", "--source-root", str(src)]) == 0

    target, status = _opencode_target(capsys.readouterr().out)
    expected = tmp_path / "xdg" / "opencode" / "AGENTS.md"
    assert target == expected, f"expected {expected}, got {target}"
    assert status == "created"
    assert target.is_file()
    assert "STANDARDS-BODY" in target.read_text(encoding="utf-8")


def test_opencode_target_falls_back_to_home_when_xdg_unset(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    src = _make_source(tmp_path / "src")

    assert cli.main(["--tools", "opencode", "--source-root", str(src)]) == 0

    target, status = _opencode_target(capsys.readouterr().out)
    expected = tmp_path / "home" / ".config" / "opencode" / "AGENTS.md"
    assert target == expected, f"expected {expected}, got {target}"
    assert status == "created"
    assert target.is_file()


# --- guards on the surrounding work -----------------------------------------

def test_cli_suite_is_hermetic_under_xdg_config_home(tmp_path) -> None:
    """The CLI's own suite must pass where CI runs it: with XDG set."""
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
    }
    env.pop("OPENCODE_CONFIG_DIR", None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-n0", "-p", "no:cacheprovider", CLI_SUITE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-4000:]


def test_reference_no_longer_claims_xdg_is_not_forwarded() -> None:
    reference = (REPO_ROOT / "REFERENCE.md").read_text(encoding="utf-8")
    assert "XDG_CONFIG_HOME" in reference
    assert "deliberately does not forward" not in reference
