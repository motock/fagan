"""Tests for the LD90 repo filter on the local first-pass-clean report CLI.

The CLI is driven through ``main([...])``; every fixture is built under
``tmp_path`` so nothing reads or writes the real ``~/.claude/plans``.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from scripts.local_success_report import main

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "local_success_report.py"
ROOT = Path(__file__).resolve().parents[2]


def _story(**over):
    """A story that is clean, in population, on-device and dispatched."""
    return {
        "status": "done",
        "backend": "ollama",
        "dispatched_at": "2026-01-01T00:00:00Z",
        "dispatched_model": "m",
        **over,
    }


def _manifest(plan_dir, plan, stories, repo_root=ROOT):
    """Write ``<plan>.manifest.json``; ``repo_root=None`` omits the field."""
    payload = {"stories": stories}
    if repo_root is not None:
        payload["repo_root"] = str(repo_root)
    (plan_dir / f"{plan}.manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def _run(capsys, plan_dir, *args):
    """Run the CLI, assert success, and return its stdout."""
    assert main(["--plan-dir", str(plan_dir), *args]) == 0
    return capsys.readouterr().out


def _overall(out):
    match = re.search(r"^overall: \d+/\d+ clean \(.+\)$", out, re.MULTILINE)
    assert match, f"no overall block in output:\n{out}"
    return match.group(0)


def _cohort_count(out):
    match = re.search(r"^cohort: .* \((\d+) stories\)$", out, re.MULTILINE)
    assert match, f"no cohort line in output:\n{out}"
    return int(match.group(1))


def _repo_line(out):
    """The active-filter line, which must sit directly after ``window:``."""
    lines = out.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("window:"))
    assert index + 1 < len(lines), out
    assert lines[index + 1].startswith("repo:"), out
    return lines[index + 1]


def _repo_path(out):
    return Path(_repo_line(out).split(":", 1)[1].strip()).resolve()


def test_default_filter_reports_only_this_repo(tmp_path, capsys):
    _manifest(tmp_path, "mine", {"s1": _story()}, repo_root=ROOT)
    _manifest(tmp_path, "theirs", {"s2": _story(), "s3": _story()}, repo_root=tmp_path / "other")
    out = _run(capsys, tmp_path)
    assert _overall(out) == "overall: 1/1 clean (100.0%)"
    assert _cohort_count(out) == 1
    assert _repo_path(out) == ROOT


def test_repo_flag_restricts_to_matching_manifest(tmp_path, capsys):
    repo_a, repo_b = tmp_path / "repo-a", tmp_path / "repo-b"
    _manifest(tmp_path, "a", {"s1": _story()}, repo_root=repo_a)
    _manifest(tmp_path, "b", {"s2": _story(), "s3": _story(status="parked")}, repo_root=repo_b)

    out = _run(capsys, tmp_path, "--repo", str(repo_a))
    assert _overall(out) == "overall: 1/1 clean (100.0%)"
    assert _cohort_count(out) == 1
    assert _repo_path(out) == repo_a.resolve()

    out = _run(capsys, tmp_path, "--repo", str(repo_b))
    assert _overall(out) == "overall: 1/2 clean (50.0%)"
    assert _cohort_count(out) == 2
    assert _repo_path(out) == repo_b.resolve()


def test_all_repos_includes_every_manifest(tmp_path, capsys):
    _manifest(tmp_path, "a", {"s1": _story()}, repo_root=tmp_path / "repo-a")
    _manifest(tmp_path, "b", {"s2": _story(), "s3": _story(status="parked")}, repo_root=tmp_path / "repo-b")
    out = _run(capsys, tmp_path, "--all-repos")
    assert _overall(out) == "overall: 2/3 clean (66.7%)"
    assert _cohort_count(out) == 3
    assert _repo_line(out) == "repo: all"


def test_missing_repo_root_is_unattributable(tmp_path, capsys):
    _manifest(tmp_path, "mine", {"s1": _story()}, repo_root=ROOT)
    _manifest(tmp_path, "orphan", {"s2": _story()}, repo_root=None)

    out = _run(capsys, tmp_path)
    assert _overall(out) == "overall: 1/1 clean (100.0%)"
    assert _cohort_count(out) == 1

    out = _run(capsys, tmp_path, "--repo", str(ROOT))
    assert _overall(out) == "overall: 1/1 clean (100.0%)"
    assert _cohort_count(out) == 1

    out = _run(capsys, tmp_path, "--all-repos")
    assert _overall(out) == "overall: 2/2 clean (100.0%)"
    assert _cohort_count(out) == 2


def test_repo_with_no_matching_manifests_is_na(tmp_path, capsys):
    _manifest(tmp_path, "mine", {"s1": _story()}, repo_root=ROOT)
    empty = tmp_path / "empty-repo"
    out = _run(capsys, tmp_path, "--repo", str(empty))
    assert _overall(out) == "overall: 0/0 clean (n/a)"
    assert _repo_path(out) == empty.resolve()


def test_repo_paths_are_resolved_before_comparison(tmp_path, capsys):
    repo_a, repo_b = tmp_path / "repo-a", tmp_path / "repo-b"
    (tmp_path / "a.manifest.json").write_text(
        json.dumps({"stories": {"s1": _story()}, "repo_root": f"{repo_a}/./"}),
        encoding="utf-8",
    )
    (tmp_path / "b.manifest.json").write_text(
        json.dumps({"stories": {"s2": _story()}, "repo_root": str(repo_b)}),
        encoding="utf-8",
    )
    out = _run(capsys, tmp_path, "--repo", str(repo_a / ".." / "repo-a" / "."))
    assert _overall(out) == "overall: 1/1 clean (100.0%)"
    assert _cohort_count(out) == 1


def test_invalid_manifest_warns_and_is_skipped(tmp_path, capsys):
    (tmp_path / "broken.manifest.json").write_text("{", encoding="utf-8")
    (tmp_path / "adir.manifest.json").mkdir()
    _manifest(tmp_path, "mine", {"s1": _story()}, repo_root=ROOT)

    for args in ((), ("--all-repos",)):
        assert main(["--plan-dir", str(tmp_path), *args]) == 0
        captured = capsys.readouterr()
        assert "broken" in captured.err
        assert "adir" in captured.err
        assert _overall(captured.out) == "overall: 1/1 clean (100.0%)"


def test_docstring_documents_repo_flags():
    docstring = ast.get_docstring(ast.parse(SCRIPT.read_text(encoding="utf-8"))) or ""
    assert re.search(r"--repo\b", docstring), docstring
    assert re.search(r"--all-repos\b", docstring), docstring
