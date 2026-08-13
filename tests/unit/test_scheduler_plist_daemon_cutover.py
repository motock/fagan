"""Tests for the advance-scheduler launchd plist daemon cutover.

This story converts the advance-scheduler launchd agent from a 60s one-shot
Python ``-c`` tick into a long-lived daemon (``pipeline.scheduler_daemon``)
that owns its own cadence. launchd's remaining job is crash-restart only, so
``StartInterval`` is removed and ``KeepAlive`` is added in its place.

The plist files are asserted as TEXT (not via ``plistlib``) because
``plistlib.load`` raises on the ``--`` sequences inside the XML comments of
the sibling plists, and a "regenerate and compare" test cannot hold inside a
git worktree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

LAUNCHD_DIR = Path(__file__).resolve().parents[2] / "launchd"

ADVANCE_TEMPLATE = LAUNCHD_DIR / "com.claude.pipeline.advance-scheduler.plist.template"
ADVANCE_PLIST = LAUNCHD_DIR / "com.claude.pipeline.advance-scheduler.plist"
MLX_PLIST = LAUNCHD_DIR / "com.claude.pipeline.mlx-supervisor.plist"

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
REFERENCE = REPO_ROOT / "REFERENCE.md"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def advance_template_text() -> str:
    return ADVANCE_TEMPLATE.read_text()


@pytest.fixture(scope="module")
def advance_plist_text() -> str:
    return ADVANCE_PLIST.read_text()


@pytest.fixture(scope="module")
def mlx_plist_text() -> str:
    return MLX_PLIST.read_text()


def _doc_text() -> str:
    """Return the scheduler documentation text, preferring README then REFERENCE."""
    if README.exists() and _has_scheduler_section(README.read_text()):
        return README.read_text()
    return REFERENCE.read_text()


def _has_scheduler_section(text: str) -> bool:
    lowered = text.lower()
    return "scheduler" in lowered and (
        "advance-scheduler" in lowered or "advance_scheduler" in lowered
    )


# --------------------------------------------------------------------------- #
# ProgramArguments: daemon module invocation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "plist_path",
    [ADVANCE_TEMPLATE, ADVANCE_PLIST],
    ids=["template", "generated"],
)
def test_program_arguments_invoke_daemon_module(plist_path: Path) -> None:
    """ProgramArguments must run ``-m pipeline.scheduler_daemon``, not a ``-c`` one-shot."""
    text = plist_path.read_text()
    assert "pipeline.scheduler_daemon" in text, (
        f"{plist_path.name}: ProgramArguments must invoke 'pipeline.scheduler_daemon'"
    )


@pytest.mark.parametrize(
    "plist_path",
    [ADVANCE_TEMPLATE, ADVANCE_PLIST],
    ids=["template", "generated"],
)
def test_program_arguments_use_dash_m_not_dash_c(plist_path: Path) -> None:
    """The ``-c`` one-shot flag must be gone, replaced by ``-m``."""
    text = plist_path.read_text()
    assert "<string>-m</string>" in text, (
        f"{plist_path.name}: ProgramArguments must contain a '-m' module flag"
    )
    assert "<string>-c</string>" not in text, (
        f"{plist_path.name}: the old '-c' one-shot flag must be removed"
    )


@pytest.mark.parametrize(
    "plist_path",
    [ADVANCE_TEMPLATE, ADVANCE_PLIST],
    ids=["template", "generated"],
)
def test_old_one_shot_command_gone(plist_path: Path) -> None:
    """The old ``import app.pipeline_mcp_server`` one-shot command must be gone."""
    text = plist_path.read_text()
    assert "advance_all_plans()" not in text, (
        f"{plist_path.name}: the old one-shot 'advance_all_plans()' command must be removed"
    )
    assert "import app.pipeline_mcp_server" not in text, (
        f"{plist_path.name}: the old one-shot import must be removed"
    )


# --------------------------------------------------------------------------- #
# StartInterval removed, KeepAlive added
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "plist_path",
    [ADVANCE_TEMPLATE, ADVANCE_PLIST],
    ids=["template", "generated"],
)
def test_no_start_interval_key(plist_path: Path) -> None:
    """Neither plist may carry a StartInterval key (no second clock alongside the daemon)."""
    text = plist_path.read_text()
    assert "<key>StartInterval</key>" not in text, (
        f"{plist_path.name}: StartInterval key must be removed"
    )


@pytest.mark.parametrize(
    "plist_path",
    [ADVANCE_TEMPLATE, ADVANCE_PLIST],
    ids=["template", "generated"],
)
def test_keep_alive_key_present_and_true(plist_path: Path) -> None:
    """KeepAlive must be present and set to true (crash-restart only)."""
    text = plist_path.read_text()
    assert "<key>KeepAlive</key>" in text, (
        f"{plist_path.name}: KeepAlive key must be present"
    )
    # KeepAlive true is rendered as <true/> immediately after the key.
    assert re.search(r"<key>KeepAlive</key>\s*<true/>", text), (
        f"{plist_path.name}: KeepAlive must be set to <true/>"
    )


@pytest.mark.parametrize(
    "plist_path",
    [ADVANCE_TEMPLATE, ADVANCE_PLIST],
    ids=["template", "generated"],
)
def test_no_start_interval_integer_value(plist_path: Path) -> None:
    """The old ``<integer>60</integer>`` StartInterval value must be gone."""
    text = plist_path.read_text()
    # The 60 integer that belonged to StartInterval must no longer appear as a
    # bare integer element. (Other env-var strings may contain '60' but not as
    # an <integer> element.)
    assert "<integer>60</integer>" not in text, (
        f"{plist_path.name}: the StartInterval <integer>60</integer> value must be removed"
    )


# --------------------------------------------------------------------------- #
# Regression guard: preserved keys
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "plist_path",
    [ADVANCE_TEMPLATE, ADVANCE_PLIST],
    ids=["template", "generated"],
)
def test_preserved_keys_present(plist_path: Path) -> None:
    """RunAtLoad, WorkingDirectory and both Standard*Path entries must remain."""
    text = plist_path.read_text()
    for key in (
        "<key>RunAtLoad</key>",
        "<key>WorkingDirectory</key>",
        "<key>StandardOutPath</key>",
        "<key>StandardErrorPath</key>",
    ):
        assert key in text, f"{plist_path.name}: preserved key {key!r} must remain"


@pytest.mark.parametrize(
    "plist_path",
    [ADVANCE_TEMPLATE, ADVANCE_PLIST],
    ids=["template", "generated"],
)
def test_abandon_process_group_present(plist_path: Path) -> None:
    """AbandonProcessGroup must remain byte-identical."""
    text = plist_path.read_text()
    assert "<key>AbandonProcessGroup</key>" in text, (
        f"{plist_path.name}: AbandonProcessGroup must remain"
    )


@pytest.mark.parametrize(
    "plist_path",
    [ADVANCE_TEMPLATE, ADVANCE_PLIST],
    ids=["template", "generated"],
)
def test_environment_variables_block_intact(plist_path: Path) -> None:
    """The entire EnvironmentVariables block must remain, untouched."""
    text = plist_path.read_text()
    assert "<key>EnvironmentVariables</key>" in text, (
        f"{plist_path.name}: EnvironmentVariables block must remain"
    )
    # Spot-check a few env vars that must not be retuned.
    for env in (
        "PIPELINE_AUTONOMY",
        "PIPELINE_LOCAL_MAX_STEPS",
        "PIPELINE_MAX_CONCURRENT_AGENTS",
        "REPO_ROOT",
    ):
        assert f"<key>{env}</key>" in text, (
            f"{plist_path.name}: env var {env} must remain in EnvironmentVariables"
        )


@pytest.mark.parametrize(
    "plist_path",
    [ADVANCE_TEMPLATE, ADVANCE_PLIST],
    ids=["template", "generated"],
)
def test_log_paths_unchanged(plist_path: Path) -> None:
    """The advance-scheduler log file names must be unchanged."""
    text = plist_path.read_text()
    assert "advance-scheduler.log" in text, (
        f"{plist_path.name}: StandardOutPath log name must remain"
    )
    assert "advance-scheduler.err.log" in text, (
        f"{plist_path.name}: StandardErrorPath log name must remain"
    )


# --------------------------------------------------------------------------- #
# Template / generated agreement
# --------------------------------------------------------------------------- #


def _daemon_relevant_lines(text: str) -> set[str]:
    """Return the set of lines relevant to the daemon cutover for comparison."""
    relevant = set()
    for line in text.splitlines():
        stripped = line.strip()
        if any(
            marker in stripped
            for marker in (
                "pipeline.scheduler_daemon",
                "<string>-m</string>",
                "<string>-c</string>",
                "<key>KeepAlive</key>",
                "<key>StartInterval</key>",
                "<true/>",
                "<integer>60</integer>",
                "advance_all_plans()",
                "import app.pipeline_mcp_server",
            )
        ):
            relevant.add(stripped)
    return relevant


def test_template_and_generated_agree_on_cutover(
    advance_template_text: str, advance_plist_text: str
) -> None:
    """The template and generated plist must agree on all four cutover points."""
    tmpl = _daemon_relevant_lines(advance_template_text)
    gen = _daemon_relevant_lines(advance_plist_text)
    assert tmpl == gen, (
        "template and generated plist diverge on the daemon cutover:\n"
        f"only in template: {tmpl - gen}\n"
        f"only in generated: {gen - tmpl}"
    )


def test_template_and_generated_both_daemon(advance_template_text: str, advance_plist_text: str) -> None:
    """Both files independently contain the daemon module name."""
    assert "pipeline.scheduler_daemon" in advance_template_text
    assert "pipeline.scheduler_daemon" in advance_plist_text


def test_template_and_generated_both_keepalive(advance_template_text: str, advance_plist_text: str) -> None:
    """Both files independently contain KeepAlive true."""
    for label, text in (("template", advance_template_text), ("generated", advance_plist_text)):
        assert re.search(r"<key>KeepAlive</key>\s*<true/>", text), (
            f"{label}: KeepAlive must be set true"
        )


def test_template_and_generated_neither_startinterval(
    advance_template_text: str, advance_plist_text: str
) -> None:
    """Neither file contains a StartInterval key."""
    assert "<key>StartInterval</key>" not in advance_template_text
    assert "<key>StartInterval</key>" not in advance_plist_text


# --------------------------------------------------------------------------- #
# Scope guard: mlx-supervisor untouched
# --------------------------------------------------------------------------- #


def test_mlx_supervisor_still_has_start_interval(mlx_plist_text: str) -> None:
    """The mlx-supervisor plist must still carry its StartInterval (edit was scoped)."""
    assert "<key>StartInterval</key>" in mlx_plist_text, (
        "mlx-supervisor plist must retain its StartInterval (legitimately interval-driven)"
    )


def test_mlx_supervisor_start_interval_is_120(mlx_plist_text: str) -> None:
    """The mlx-supervisor StartInterval value (120) must be untouched."""
    assert re.search(r"<key>StartInterval</key>\s*<integer>120</integer>", mlx_plist_text), (
        "mlx-supervisor plist must retain its 120s StartInterval value"
    )


def test_mlx_supervisor_not_converted_to_daemon(mlx_plist_text: str) -> None:
    """The mlx-supervisor must NOT have been converted to the scheduler daemon."""
    assert "pipeline.scheduler_daemon" not in mlx_plist_text, (
        "mlx-supervisor plist must not reference pipeline.scheduler_daemon"
    )


# --------------------------------------------------------------------------- #
# Documentation
# --------------------------------------------------------------------------- #


def test_documentation_names_both_env_vars() -> None:
    """The scheduler documentation must name both daemon env vars."""
    text = _doc_text()
    assert "PIPELINE_SCHEDULER_INTERVAL_S" in text, (
        "documentation must mention PIPELINE_SCHEDULER_INTERVAL_S"
    )
    assert "PIPELINE_SCHEDULER_HEALTH_PATH" in text, (
        "documentation must mention PIPELINE_SCHEDULER_HEALTH_PATH"
    )


def test_documentation_describes_daemon_not_tick() -> None:
    """The docs must describe the advance scheduler as a long-lived daemon, not a 60s tick."""
    text = _doc_text()
    lowered = text.lower()
    assert "daemon" in lowered, (
        "documentation must describe the advance scheduler as a daemon"
    )


def test_documentation_states_default_interval_60() -> None:
    """The docs must record the default reconcile interval of 60 seconds."""
    text = _doc_text()
    # Look for the default near the env var name.
    idx = text.find("PIPELINE_SCHEDULER_INTERVAL_S")
    assert idx != -1, "documentation must mention PIPELINE_SCHEDULER_INTERVAL_S"
    window = text[idx : idx + 400]
    assert "60" in window, (
        "documentation must state the default value (60) for PIPELINE_SCHEDULER_INTERVAL_S"
    )


def test_documentation_states_health_path_optional() -> None:
    """The docs must state that PIPELINE_SCHEDULER_HEALTH_PATH is optional."""
    text = _doc_text()
    idx = text.find("PIPELINE_SCHEDULER_HEALTH_PATH")
    assert idx != -1, "documentation must mention PIPELINE_SCHEDULER_HEALTH_PATH"
    window = text[idx : idx + 400].lower()
    assert "optional" in window, (
        "documentation must state that PIPELINE_SCHEDULER_HEALTH_PATH is optional"
    )