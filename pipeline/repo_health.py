"""Deterministic, measured repo‑health probes for the failure‑triage layer.

The contract is that all functions are fail‑safe: they return a finding dict or
``None`` and never raise.  The ``repo_issue`` class is deterministic and
must never be asked of a model (C7).  This module implements a lint baseline
probe that runs a detected lint command and reports failures.

Additionally this module provides oracle and CI finding helpers.

"""

import os
import subprocess
from pathlib import Path

from .build_detect import detect_lint_command, detect_test_command

# Import oracle gate at module level
from .oracle_gate import validate_acceptance_fixtures

__all__ = ["ci_finding", "lint_baseline_finding", "oracle_finding", "suite_baseline_finding"]


def lint_baseline_finding(checkout: str | Path, timeout_s: int = 300) -> dict | None:
    """Run a lint baseline probe on *checkout*.

    Parameters
    ----------
    checkout:
        Path to the repository checkout.  The function accepts a string or a
        :class:`pathlib.Path`.
    timeout_s:
        Maximum number of seconds to allow the lint command to run.

    Returns
    -------
    dict | None
        ``None`` if the repo declares no lint signal or the lint command
        succeeds.  Otherwise a dict with keys ``kind``, ``detail`` and
        ``command``.
    """
    if timeout_s <= 0:
        return {"kind":"lint_probe_failed","detail":"timeout must be positive","command":""}
    try:
        detected = detect_lint_command(Path(checkout))
        if detected is None:
            return None
        cwd, cmd = detected
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
        if result.returncode == 0:
            return None
        # Combine stdout and stderr, truncate to last 800 chars.
        output = (result.stdout or "") + (result.stderr or "")
        detail = output[-800:]
        return {
            "kind": "lint_baseline_red",
            "detail": detail,
            "command": " ".join(cmd),
        }
    except (OSError, subprocess.SubprocessError) as exc:  # includes TimeoutExpired
        return {
            "kind": "lint_probe_failed",
            "detail": f"{type(exc).__name__}: {exc}",
            "command": "",
        }


def suite_baseline_finding(checkout: str | Path, timeout_s: int = 600) -> dict | None:
    gate = os.environ.get("PIPELINE_TRIAGE_SUITE_PROBE", "")
    gate = gate.strip().lower()
    if gate not in ("1", "true", "yes", "on"):
        return None
    """Run a suite baseline probe on *checkout*.

    The full test suite in this repository takes roughly eight minutes.  The
    triage sweep runs inside the scheduler tick, and an always‑on suite probe
    would stall every tick for every plan.  Therefore this probe is opt-in
    via the ``PIPELINE_TRIAGE_SUITE_PROBE`` environment variable.
    """
    suite_baseline_finding.__doc__ = """Run a suite baseline probe on *checkout*.

    The full test suite in this repository takes roughly eight minutes.  The
    triage sweep runs inside the scheduler tick, and an always‑on suite probe
    would stall every tick for every plan.  Therefore this probe is opt-in
    via the ``PIPELINE_TRIAGE_SUITE_PROBE`` environment variable.
    """
    try:
        test_dir, test_cmd = detect_test_command(Path(checkout))
        result = subprocess.run(
            test_cmd,
            cwd=test_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
        if result.returncode in (0, 5):
            return None
        output = (result.stdout or "") + (result.stderr or "")
        detail = output[-800:]
        return {
            "kind": "suite_baseline_red",
            "detail": detail,
            "command": " ".join(test_cmd),
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "kind": "suite_probe_failed",
            "detail": f"{type(exc).__name__}: {exc}",
            "command": "",
        }


def oracle_finding(story, checkout) -> dict | None:
    """Return an oracle finding dict or None.

    Parameters
    ----------
    story
        The story dict passed to :func:`pipeline.oracle_gate.validate_acceptance_fixtures`.
    checkout
        Path to the repository checkout.

    Returns
    -------
    dict | None
        ``None`` if the story has no acceptance fixtures, fails correctly, or the
        state is unrecognised.  Otherwise a finding dict with keys ``kind``,
        ``detail`` and ``paths``.
    """
    try:
        result = validate_acceptance_fixtures(story, Path(checkout))
    except Exception as exc:  # defensive guard  # noqa: BLE001
        return {
            "kind": "oracle_probe_failed",
            "detail": f"{type(exc).__name__}: {exc}",
            "paths": [],
        }
    state = result.get("state")
    if state in ("none", "fails_correctly"):
        return None
    if state == "errors":
        return {
            "kind": "oracle_broken",
            "detail": result.get("detail", "")[-800:],
            "paths": result.get("paths", []),
        }
    if state == "passes":
        return {
            "kind": "oracle_already_passes",
            "detail": result.get("detail", ""),
            "paths": result.get("paths", []),
        }
    if state == "empty":
        return {
            "kind": "oracle_empty",
            "detail": result.get("detail", ""),
            "paths": result.get("paths", []),
        }
    return None


def ci_finding(ci_status) -> dict | None:
    """Pure CI finding helper.

    This function is deliberately free of any imports that pull in
    :mod:`pipeline.ci` or :mod:`pipeline.server` to avoid circular imports.
    It operates purely on the ``ci_status`` dict supplied by the caller.
    """
    if not isinstance(ci_status, dict):
        return None
    state = ci_status.get("state")
    if state in ("pass", "pending"):
        return None
    if state == "none":
        return {
            "kind": "ci_unavailable",
            "detail": str(ci_status.get("error", ""))[:800],
        }
    if state == "fail":
        return {
            "kind": "ci_red",
            "detail": str(ci_status.get("error", ""))[:800],
        }
    return None
