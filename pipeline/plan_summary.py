"""Human-readable completion summary for a finished plan.

``format_plan_summary`` renders the per-story and plan-level picture of a
completed plan for an outbound notification channel.  It is pure: no file
I/O, no network, no environment reads, no clock reads, and it never mutates
its arguments.

It reuses the existing metrics helpers instead of recomputing:

* ``pipeline.story_metrics.compute_story_metrics(records)`` for per-story
  counters, and
* ``pipeline.story_metrics.compute_plan_rollup(...)`` for the plan totals.

Data minimisation (hard requirement): notification ``message`` text in this
codebase can carry raw CI stderr, gate errors, branch names and absolute
worktree paths, so the summary derives ONLY counts/outcomes from the records.
No record's ``message`` value and no filesystem path (``manifest["repo_root"]``
or otherwise) is ever embedded in the returned string.

Public surface (exactly one function): ``format_plan_summary``.
"""

from __future__ import annotations

from typing import Any

from pipeline.story_metrics import compute_plan_rollup, compute_story_metrics

__all__ = ["format_plan_summary"]


def _story_line(key: str, story: dict[str, Any]) -> str:
    """Render one manifest story: key, summary, and pr_url when present.

    Missing/empty fields are omitted rather than rendered as ``None``.
    """
    line = f"- {key}"
    summary = story.get("summary")
    if isinstance(summary, str) and summary:
        line += f": {summary}"
    pr_url = story.get("pr_url")
    if isinstance(pr_url, str) and pr_url:
        line += f" (pr: {pr_url})"
    return line


def _rollup_lines(rollup: dict[str, Any]) -> list[str]:
    """Render every non-None number ``compute_plan_rollup`` returned."""
    return [
        f"- {name}={value}"
        for name, value in rollup.items()
        if value is not None
    ]


def format_plan_summary(plan_name: str, manifest: dict, records: list[dict]) -> str:
    """Render a human-readable completion summary for a finished plan.

    ``manifest`` is the plan manifest as stored in PLAN_DIR; ``records`` are
    the structured notification records for the plan.  Only counts and
    outcomes are taken from the records (via the story-metrics helpers) —
    never their raw ``message`` text — and no filesystem path is emitted.
    """
    stories = manifest.get("stories") or {}
    metrics = compute_story_metrics(records)
    rollup = compute_plan_rollup(list(metrics.values()))

    lines: list[str] = [
        f"Plan: {plan_name}",
        f"Stories: {len(stories)}",
        "",
    ]

    if stories:
        lines.append("Per story:")
        # sorted() builds a new list; the caller's manifest is not mutated.
        for key in sorted(stories):
            story = stories.get(key)
            if not isinstance(story, dict):
                story = {}
            lines.append(_story_line(key, story))
        lines.append("")

    lines.append("Plan rollup:")
    lines.extend(_rollup_lines(rollup))
    return "\n".join(lines)