"""Acceptance oracle: CI must exercise macOS as well as Linux.

Dispatch, the check_story_status done-bar and the merge-gate reverify all run
on macOS while CI runs ubuntu-latest only, so a macOS-only dependency passes
every local gate and fails only after the PR is open (observed via `plutil`).
"""
import re
from pathlib import Path

import pytest

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"

# Parsed as text on purpose: PyYAML is not a dependency of this repo, and an
# importorskip would turn this whole oracle into a silent skip (returncode 0),
# i.e. a fixture that grades nothing.
_LABEL_RE = re.compile("((?:macos|ubuntu|windows)[A-Za-z0-9_.-]*)")
_JOB_RE = re.compile("^  ([A-Za-z0-9_-]+):[ ]*$", re.MULTILINE)


def _text():
    return re.sub("#.*", "", _WORKFLOW.read_text())


def _job_blocks():
    text = _text()
    starts = [(m.start(), m.group(1)) for m in _JOB_RE.finditer(text)]
    blocks = {}
    for i, (pos, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        blocks[name] = text[pos:end]
    return blocks


def _labels(block):
    """Runner labels declared in a job block, from `runs-on:` values and from
    matrix list entries. Line-scoped so an unrelated mention elsewhere in the
    block (or in a step name) is not mistaken for a runner."""
    found = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith(("runs-on:", "- ")):
            found.extend(_LABEL_RE.findall(stripped))
    return found


def test_the_workflow_defines_jobs():
    assert _job_blocks(), "could not parse any job block out of ci.yml"


# The macOS CI leg is intentionally disabled while the repo is private
# (commit 7958a23, "ci: disable macOS runner leg until repo is public").
# This oracle is suspended for the same period: it would otherwise fail the
# suite on every PR, masking real regressions. Re-arm it — re-enable the
# macOS runner leg in .github/workflows/ci.yml AND remove this skip — the
# moment the repo goes public, so macOS-only dependencies are caught before
# a PR opens again (the original motivation, observed via `plutil`).
_MACOS_LEG_DISABLED_WHILE_PRIVATE = (
    "macOS CI leg intentionally disabled while the repo is private "
    "(commit 7958a23, 'until repo is public'); re-enable the macOS runner "
    "leg in ci.yml and remove this skip when the repo goes public"
)


@pytest.mark.skip(reason=_MACOS_LEG_DISABLED_WHILE_PRIVATE)
def test_a_job_that_runs_pytest_also_runs_on_macos():
    offenders = {
        name: _labels(block)
        for name, block in _job_blocks().items()
        if "pytest" in block
    }
    assert offenders, "no job in ci.yml runs pytest"
    assert any(
        any(label.startswith("macos") for label in labels)
        for labels in offenders.values()
    ), (
        "no pytest-running CI job targets macOS; dispatch and the merge gate run "
        "on macOS, so a macOS-only dependency cannot be caught before the PR is "
        f"open (pytest jobs and their runners: {offenders})"
    )


def test_linux_coverage_is_retained():
    labels = [
        label
        for block in _job_blocks().values()
        if "pytest" in block
        for label in _labels(block)
    ]
    assert any(label.startswith("ubuntu") for label in labels), (
        "ubuntu coverage must not be dropped in favour of macOS"
    )


def test_the_full_suite_override_is_still_used():
    text = _WORKFLOW.read_text()
    assert "--override-ini=testpaths=" in text, (
        "the testpaths allowlist override (PR #199) must survive this change"
    )
