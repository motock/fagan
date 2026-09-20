"""Tests for the LD90 shared-cohort fix in the local success report CLI.

The report used to call ``rolling_rate`` once per tier with the same
``--window`` value, and ``rolling_rate`` filters by tier *before* slicing
the last N stories.  Each tier block therefore covered a different stretch
of time, so the tier rates did not partition the overall rate.

These tests drive the real CLI through ``main([...])`` and pin the fixed
behaviour: one cohort (the newest N population stories) is selected once,
and every block -- overall and each tier -- is measured over that cohort.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.local_success_report import main

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "local_success_report.py"
TIERS = ("on-device", "cloud-oss", "unknown")


def _story(**over):
    """A clean, in-population, on-device, dispatched story."""
    return {
        "status": "done",
        "backend": "ollama",
        "dispatched_at": "2026-01-01T00:00:00Z",
        "dispatched_model": "m",
        **over,
    }


def _plan(plan_dir, plan, stories, records=None):
    """Write ``<plan>.manifest.json`` and, when given, its JSONL sidecar."""
    (plan_dir / f"{plan}.manifest.json").write_text(
        json.dumps({"stories": stories}), encoding="utf-8"
    )
    if records is not None:
        (plan_dir / f"{plan}.notifications.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
        )


def _run(capsys, plan_dir, *args):
    """Run the CLI, assert success, and return its stdout."""
    assert main(["--plan-dir", str(plan_dir), *args]) == 0
    return capsys.readouterr().out


def _blocks(out):
    """Map each unindented ``<label>: ... clean (...)`` header to its block."""
    blocks, current = {}, None
    for line in out.splitlines():
        if line[:1].isspace():
            if current and line.strip():
                blocks[current]["reasons"].append(line.strip())
            continue
        label, sep, rest = line.strip().partition(":")
        current = label if sep and " clean (" in rest else None
        if current:
            blocks[current] = {"header": line.strip(), "reasons": []}
    return blocks


def _header(out, label):
    blocks = _blocks(out)
    assert label in blocks, f"no {label!r} block in output:\n{out}"
    return blocks[label]["header"]


def _counts(out, label):
    """Return ``(clean, count)`` parsed from a block header."""
    header = _header(out, label)
    match = re.fullmatch(rf"{re.escape(label)}: (\d+)/(\d+) clean \(.+\)", header)
    assert match, header
    return int(match.group(1)), int(match.group(2))


def _reasons(out, label="overall"):
    blocks = _blocks(out)
    assert label in blocks, f"no {label!r} block in output:\n{out}"
    return blocks[label]["reasons"]


def _cohort_lines(out):
    return [ln for ln in out.splitlines() if ln.startswith("cohort:")]


def _nonblank(out):
    return [ln for ln in out.splitlines() if ln.strip()]


# --------------------------------------------------------------------------
# The cohort itself: every block is measured over the same newest stories.
# --------------------------------------------------------------------------


def test_every_tier_is_measured_over_the_same_newest_stories(tmp_path, capsys):
    """With ``--window 1`` the single newest story is the whole cohort.

    Per-tier windowing would slice the cloud-oss tier on its own and report
    ``cloud-oss: 1/1``; the shared cohort must report ``0/0`` for it.
    """
    _plan(
        tmp_path,
        "alpha",
        {
            "cloud_old": _story(
                dispatched_model="glm-5.3-flash:cloud",
                dispatched_at="2026-01-01T00:00:00Z",
            ),
            "dev_new": _story(dispatched_at="2026-02-01T00:00:00Z"),
        },
    )
    out = _run(capsys, tmp_path, "--window", "1")
    assert _counts(out, "on-device") == (1, 1)
    assert _counts(out, "cloud-oss") == (0, 0)
    assert _counts(out, "overall") == (1, 1)


def test_the_blocks_partition_the_overall_block(tmp_path, capsys):
    """The tier counts must sum to the overall count."""
    _plan(
        tmp_path,
        "alpha",
        {
            "a": _story(status="parked", dispatched_at="2026-01-01T00:00:00Z"),
            "b": _story(
                dispatched_model="m:cloud", dispatched_at="2026-01-02T00:00:00Z"
            ),
            "c": _story(dispatched_at="2026-01-03T00:00:00Z"),
            "d": _story(
                dispatched_model="m:cloud", dispatched_at="2026-01-04T00:00:00Z"
            ),
            "e": _story(dispatched_at="2026-01-05T00:00:00Z"),
        },
    )
    out = _run(capsys, tmp_path, "--window", "3")
    assert _counts(out, "overall") == (3, 3)
    assert _counts(out, "on-device") == (2, 2)
    assert _counts(out, "cloud-oss") == (1, 1)
    assert _counts(out, "unknown") == (0, 0)
    tier_total = sum(_counts(out, tier)[1] for tier in TIERS)
    assert tier_total == _counts(out, "overall")[1] == 3


def test_window_zero_keeps_every_population_story(tmp_path, capsys):
    """``--window 0`` means all stories, and out-of-population ones never count."""
    _plan(
        tmp_path,
        "alpha",
        {
            "s1": _story(dispatched_at="2026-01-01T00:00:00Z"),
            "s2": _story(dispatched_at="2026-01-02T00:00:00Z"),
            "claude": _story(backend="claude", dispatched_at="2026-01-03T00:00:00Z"),
        },
    )
    out = _run(capsys, tmp_path, "--window", "0")
    assert _counts(out, "overall") == (2, 2)
    lines = _cohort_lines(out)
    assert len(lines) == 1, out
    assert "2 stories" in lines[0]


def test_a_story_without_dispatched_at_is_not_part_of_the_cohort(tmp_path, capsys):
    """An undated story cannot be ordered, so it is excluded from the cohort."""
    _plan(
        tmp_path,
        "alpha",
        {
            "undated": _story(dispatched_at=None),
            "dated": _story(dispatched_at="2026-01-02T00:00:00Z"),
        },
    )
    out = _run(capsys, tmp_path, "--window", "0")
    assert _counts(out, "overall") == (1, 1)
    lines = _cohort_lines(out)
    assert len(lines) == 1, out
    assert "1 stories" in lines[0]


def test_a_story_outside_the_cohort_contributes_no_reasons(tmp_path, capsys):
    """Reasons come from the cohort only, never from older stories."""
    _plan(
        tmp_path,
        "alpha",
        {
            "old_parked": _story(
                status="parked", dispatched_at="2026-01-01T00:00:00Z"
            ),
            "new_clean": _story(dispatched_at="2026-02-01T00:00:00Z"),
        },
    )
    out = _run(capsys, tmp_path, "--window", "1")
    assert _counts(out, "overall") == (1, 1)
    assert _reasons(out, "overall") == []


def test_no_population_stories_prints_no_cohort_line(tmp_path, capsys):
    """An empty cohort prints no cohort line and an ``n/a`` overall block."""
    _plan(tmp_path, "alpha", {"claude": _story(backend="claude")})
    out = _run(capsys, tmp_path, "--window", "0")
    assert "cohort:" not in out
    assert _counts(out, "overall") == (0, 0)
    assert _header(out, "overall") == "overall: 0/0 clean (n/a)"


# --------------------------------------------------------------------------
# The cohort line: where it sits and what it names.
# --------------------------------------------------------------------------


def test_the_cohort_line_names_the_selected_span_and_size(tmp_path, capsys):
    _plan(
        tmp_path,
        "alpha",
        {
            "s1": _story(dispatched_at="2026-01-01T00:00:00Z"),
            "s2": _story(dispatched_at="2026-01-02T00:00:00Z"),
            "s3": _story(dispatched_at="2026-01-03T00:00:00Z"),
        },
    )
    out = _run(capsys, tmp_path, "--window", "2")
    lines = _cohort_lines(out)
    assert len(lines) == 1, out
    assert "2026-01-02T00:00:00Z" in lines[0]
    assert "2026-01-03T00:00:00Z" in lines[0]
    assert "2026-01-01T00:00:00Z" not in lines[0]
    assert "2 stories" in lines[0]
    assert re.fullmatch(
        r"cohort: \S+ \.\. \S+ \(\d+ stories\)", lines[0]
    ), lines[0]


def test_the_cohort_line_follows_the_window_header(tmp_path, capsys):
    _plan(tmp_path, "alpha", {"s1": _story()})
    out = _run(capsys, tmp_path, "--window", "5")
    lines = _nonblank(out)
    assert lines[0] == "window: 5"
    assert lines[1].startswith("cohort: "), lines[:3]
    assert lines[2].startswith("overall: "), lines[:3]


def test_the_window_header_is_still_the_first_line(tmp_path, capsys):
    _plan(tmp_path, "alpha", {"s1": _story()})
    out = _run(capsys, tmp_path, "--window", "0")
    assert _nonblank(out)[0] == "window: 0"


# --------------------------------------------------------------------------
# The docstring must describe the shared cohort (and keep ``--window 0``).
# --------------------------------------------------------------------------


def _docstring_text():
    source = SCRIPT.read_text(encoding="utf-8")
    return " ".join(source.split())


def test_docstring_describes_the_shared_cohort():
    text = _docstring_text()
    assert (
        "The ``--window`` flag selects how many of the newest stories in the "
        "population to report; every tier block is computed over that same cohort"
    ) in text
    assert (
        "(``--window 0`` means all stories), so the tier rates partition the "
        "overall rate instead of each covering a different period."
    ) in text
    assert "The ``--plan-dir`` flag points to the directory" in text


def test_docstring_no_longer_claims_a_per_tier_rolling_window():
    text = _docstring_text()
    assert "The ``--window`` flag controls the size of the rolling window" not in text
    assert "--window 0" in text


# --------------------------------------------------------------------------
# The rate calls must be made over the cohort, not over all classified stories.
# --------------------------------------------------------------------------


def test_rates_are_computed_over_the_cohort():
    source = " ".join(SCRIPT.read_text(encoding="utf-8").split())
    assert "rolling_rate(cohort, window=0)" in source
    assert "rolling_rate(cohort, window=0, tier=tier)" in source
    assert "rolling_rate(all_classified" not in source
