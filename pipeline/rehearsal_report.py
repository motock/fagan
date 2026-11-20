"""Compare an overlord rehearsal with what really happened, story by story (coa-10).

Before a class is promoted on a real repository, the rehearsal stack replays a
finished plan against a throwaway mirror with full autonomy. This report reads
that plan's store records and the two git histories - the rehearsal mirror's
default branch and the reference branch that holds the real outcome - and
shows, per story, what the rehearsal did differently: whether it merged, which
files it touched beyond the reference, which rulings it made and which triage
actions it actually executed.

A story's commit is the newest commit on a branch whose full message contains
the story key as a whole word, so ``rh-1`` never matches ``rh-10``.

The report is read-only. It never writes the store, git refs or files, and
every git call runs with ``check=False`` under a timeout. A failed git query
marks that story ``git: unavailable`` and the report still runs.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from typing import Any

from .module_ref import _ModuleRef
from .store_records import InvalidRecordKeyError, validate_plan_name
from .triage_rebrief import plan_repo

#: The configured Store. Manifests and decision records are read only through
#: it, the same store ``pipeline.triage_readiness`` reads.
_store = _ModuleRef("pipeline.server", "_store")

__all__ = ["build_report", "main"]

_GIT_TIMEOUT_SECONDS = 30
_RECORD_END = "\x01"
_FIELD_END = "\x00"

_EXECUTION_QUESTION = "failure triage execution"
_RULING_QUESTION = "failure triage"
_MERGE_QUESTION = "high-risk merge adjudication"
_RULING_QUESTIONS = frozenset({_RULING_QUESTION, _MERGE_QUESTION})

#: Execution modes that did not change anything. Any other mode is executed.
_NOT_EXECUTED_MODES = frozenset({"shadow", "dry-run"})

_PARK_ACTION = "park_for_human"
_MERGE_PARK = "park"
_PARKED_STATUS = "parked"


def _git(repo: str, *args: str) -> subprocess.CompletedProcess | None:
    """Run git in ``repo``. ``None`` when git cannot run or times out."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _branch_commits(repo: str, branch: str) -> list[tuple[str, str]] | None:
    """``(sha, full message)`` for each commit on ``branch``, newest first."""
    result = _git(repo, "log", "--format=%H%x00%B%x01", branch)
    if result is None or result.returncode != 0:
        return None
    commits = []
    for chunk in result.stdout.split(_RECORD_END):
        sha, separator, message = chunk.strip("\n").partition(_FIELD_END)
        if separator and sha:
            commits.append((sha, message))
    return commits


def _newest_commit_for(commits: list[tuple[str, str]], story_key: str) -> str | None:
    """The newest commit whose message holds ``story_key`` as a whole word."""
    pattern = re.compile(rf"(?<![\w-]){re.escape(story_key)}(?![\w-])")
    for sha, message in commits:
        if pattern.search(message):
            return sha
    return None


def _files_of(repo: str, sha: str) -> set[str] | None:
    """The paths a commit touched, or ``None`` when git cannot say."""
    result = _git(repo, "show", "--name-only", "--format=", sha)
    if result is None or result.returncode != 0:
        return None
    return {line for line in result.stdout.splitlines() if line.strip()}


def _rulings_of(records: list[dict[str, Any]], story_key: str) -> list[dict[str, Any]]:
    return [
        {
            "question": record.get("question"),
            "action": record.get("action"),
            "ruling": record.get("ruling"),
            "park_class": record.get("park_class"),
            "decided_at": record.get("decided_at"),
        }
        for record in records
        if record.get("story_key") == story_key and record.get("question") in _RULING_QUESTIONS
    ]


def _executed_of(records: list[dict[str, Any]], story_key: str) -> list[dict[str, Any]]:
    return [
        {"action": record.get("action"), "result": record.get("result"), "mode": record.get("mode")}
        for record in records
        if record.get("story_key") == story_key
        and record.get("question") == _EXECUTION_QUESTION
        and record.get("mode") not in _NOT_EXECUTED_MODES
    ]


def _human_hold(story: dict[str, Any], rulings: list[dict[str, Any]]) -> bool:
    parked_for_human = story.get("status") == _PARKED_STATUS and any(
        ruling["action"] == _PARK_ACTION for ruling in rulings
    )
    held_merge = any(
        ruling["question"] == _MERGE_QUESTION and ruling["ruling"] == _MERGE_PARK
        for ruling in rulings
    )
    return parked_for_human or held_merge


def _git_view(
    repo: str,
    story_key: str,
    rehearsal_commits: list[tuple[str, str]] | None,
    reference_commits: list[tuple[str, str]] | None,
) -> dict[str, Any]:
    """The git half of one story's entry. Every field is ``None`` when git is unavailable."""
    unavailable = {
        "git": "unavailable",
        "merged": None,
        "reference_merged": None,
        "files_rehearsal": None,
        "files_reference": None,
        "files_only_rehearsal": None,
        "files_only_reference": None,
    }
    if rehearsal_commits is None or reference_commits is None:
        return unavailable
    rehearsal_sha = _newest_commit_for(rehearsal_commits, story_key)
    reference_sha = _newest_commit_for(reference_commits, story_key)
    rehearsal_files = _files_of(repo, rehearsal_sha) if rehearsal_sha else set()
    reference_files = _files_of(repo, reference_sha) if reference_sha else set()
    if rehearsal_files is None or reference_files is None:
        return unavailable
    return {
        "git": "ok",
        "merged": rehearsal_sha is not None,
        "reference_merged": reference_sha is not None,
        "files_rehearsal": sorted(rehearsal_files),
        "files_reference": sorted(reference_files),
        "files_only_rehearsal": sorted(rehearsal_files - reference_files),
        "files_only_reference": sorted(reference_files - rehearsal_files),
    }


def _story_entry(
    repo: str,
    story_key: str,
    story: dict[str, Any],
    decisions: list[dict[str, Any]],
    rehearsal_commits: list[tuple[str, str]] | None,
    reference_commits: list[tuple[str, str]] | None,
) -> dict[str, Any]:
    check = story.get("last_test_check")
    rulings = _rulings_of(decisions, story_key)
    return {
        **_git_view(repo, story_key, rehearsal_commits, reference_commits),
        "status": story.get("status"),
        "parked_reason": story.get("parked_reason"),
        "pr_url": story.get("pr_url"),
        "tests_at_merge": check.get("returncode") if isinstance(check, dict) else None,
        "rulings": rulings,
        "executed_actions": _executed_of(decisions, story_key),
        "human_hold": _human_hold(story, rulings),
    }


def _summarize(stories: dict[str, dict[str, Any]]) -> dict[str, Any]:
    def keys_where(predicate) -> list[str]:
        return sorted(key for key, entry in stories.items() if predicate(entry))

    executed = [
        {"story_key": key, **action}
        for key in sorted(stories)
        for action in stories[key]["executed_actions"]
    ]
    counts = Counter(action["action"] for action in executed)
    return {
        "merged_in_both": keys_where(lambda e: e["merged"] is True and e["reference_merged"] is True),
        "only_reference": keys_where(lambda e: e["merged"] is False and e["reference_merged"] is True),
        "only_rehearsal": keys_where(lambda e: e["merged"] is True and e["reference_merged"] is False),
        "neither": keys_where(lambda e: e["merged"] is False and e["reference_merged"] is False),
        "git_unavailable": keys_where(lambda e: e["git"] == "unavailable"),
        "executed_action_counts": dict(sorted(counts.items())),
        "executed_actions": executed,
    }


def build_report(
    plan: str,
    repo: str,
    stories: dict[str, dict[str, Any]],
    decisions: list[dict[str, Any]],
    *,
    reference: str,
    rehearsal_branch: str,
) -> dict[str, Any]:
    """The comparison for every story of ``plan``. Reads git only, never writes."""
    rehearsal_commits = _branch_commits(repo, rehearsal_branch)
    reference_commits = _branch_commits(repo, reference)
    entries = {
        key: _story_entry(repo, key, story, decisions, rehearsal_commits, reference_commits)
        for key, story in stories.items()
    }
    return {
        "plan": plan,
        "reference": reference,
        "rehearsal_branch": rehearsal_branch,
        "stories": entries,
        "summary": _summarize(entries),
    }


def _valid_ref(value: str) -> bool:
    """A ref name must not be empty, contain whitespace, or start with ``-`` (an option to git)."""
    return bool(value) and not value.startswith("-") and not any(c.isspace() for c in value)


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _print_table(report: dict[str, Any]) -> None:
    print(
        f"{'story':<14}{'status':<12}{'rehearsal':>10}{'reference':>10}"
        f"{'tests':>7}{'hold':>6}{'executed':>10}"
    )
    for key in sorted(report["stories"]):
        entry = report["stories"][key]
        print(
            f"{key:<14}{_cell(entry['status']):<12}{_cell(entry['merged']):>10}"
            f"{_cell(entry['reference_merged']):>10}{_cell(entry['tests_at_merge']):>7}"
            f"{_cell(entry['human_hold']):>6}{len(entry['executed_actions']):>10}"
        )
        if entry["git"] == "unavailable":
            print(f"  {key}: git: unavailable")
        for label, field in (
            ("files only in rehearsal", "files_only_rehearsal"),
            ("files only in reference", "files_only_reference"),
        ):
            if entry[field]:
                print(f"  {key}: {label}: {', '.join(entry[field])}")
    summary = report["summary"]
    print("summary:")
    for label in ("merged_in_both", "only_reference", "only_rehearsal", "neither", "git_unavailable"):
        print(f"  {label}: {', '.join(summary[label]) or '-'}")
    print("  executed actions by name:")
    for action, count in summary["executed_action_counts"].items():
        print(f"    {action}: {count}")
    print("  executed actions:")
    for action in summary["executed_actions"]:
        print(f"    {action['story_key']}: {action['action']} ({action['result']}, mode {action['mode']})")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Read-only. Returns 0 on a report, 1 on a refused input."""
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.rehearsal_report",
        description="Compare a rehearsal mirror's run of a plan with the reference branch.",
    )
    parser.add_argument("--plan", required=True, help="plan name to report")
    parser.add_argument("--repo", required=True, help="absolute path to the mirror's git checkout")
    parser.add_argument("--reference", required=True, help="branch or ref holding the real outcome")
    parser.add_argument(
        "--rehearsal-branch", help="branch the rehearsal merged into (default: the mirror's default branch)"
    )
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)

    try:
        plan = validate_plan_name(args.plan)
    except InvalidRecordKeyError:
        print("error: invalid plan name", file=sys.stderr)
        return 1
    repo = plan_repo({"repo_root": args.repo})
    if isinstance(repo, str):
        print("error: --repo must be the absolute root of a git repository", file=sys.stderr)
        return 1
    rehearsal_branch = args.rehearsal_branch or repo.default_branch
    if not _valid_ref(args.reference) or not _valid_ref(rehearsal_branch):
        print("error: branch names must not be empty, contain spaces, or start with '-'", file=sys.stderr)
        return 1

    manifest = _store.get_manifest_or_none(plan)
    stories = manifest.get("stories") if isinstance(manifest, dict) else None
    if not isinstance(stories, dict):
        print("error: the plan has no readable stories", file=sys.stderr)
        return 1
    try:
        decisions = _store.get_decisions(plan)
    except (OSError, TypeError, ValueError):
        print("error: a decisions log could not be read", file=sys.stderr)
        return 1

    report = build_report(
        plan,
        repo.root,
        stories,
        decisions,
        reference=args.reference,
        rehearsal_branch=rehearsal_branch,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
