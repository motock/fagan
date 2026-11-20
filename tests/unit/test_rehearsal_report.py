"""Tests for ``pipeline.rehearsal_report`` (coa-10).

The report pairs each story of a plan with the commit that landed it in the
rehearsal mirror and the commit that really landed it on the reference branch,
then lists the overlord's rulings and executed triage actions beside them. These
tests build a real throwaway git repository and stub only the store read with
fixture manifests and decision records. The store's write methods raise, so a
test fails if the report ever writes.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import pipeline.server  # noqa: F401 - pre-existing import cycle, load the server first
from pipeline import rehearsal_report as rr

PLAN = "rehearsal"
REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeStore:
    """Read methods serve fixtures; every write method raises."""

    def __init__(self, manifest, decisions):
        self._manifest = manifest
        self._decisions = decisions

    def get_manifest_or_none(self, plan_name):
        return self._manifest if plan_name == PLAN else None

    def get_decisions(self, plan_name):
        return list(self._decisions)

    def append_decision(self, plan_name, record):
        raise AssertionError("rehearsal report must not write the store")

    def save_manifest(self, plan_name, manifest):
        raise AssertionError("rehearsal report must not write the store")


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout


def _commit(repo: Path, branch: str, files: dict[str, str], message: str) -> str:
    _git(repo, "checkout", "-q", branch)
    for name, text in files.items():
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    sha = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "main")
    return sha


@pytest.fixture
def mirror(tmp_path):
    """A repo with a rehearsal ``main`` and a ``reference/main`` branch."""
    repo = tmp_path / "mirror"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "checkout", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: seed base")
    _git(repo, "branch", "reference/main")
    return repo


def _story(**fields):
    base = {"status": "merged", "parked_reason": None, "last_test_check": None, "pr_url": None}
    base.update(fields)
    return base


def _report(capsys, monkeypatch, repo, manifest, decisions):
    monkeypatch.setattr(rr, "_store", _FakeStore(manifest, decisions))
    code = rr.main(
        ["--plan", PLAN, "--repo", str(repo), "--reference", "reference/main", "--json"]
    )
    out = capsys.readouterr().out
    return code, json.loads(out) if code == 0 else None


def test_story_merged_in_both_with_same_files_has_no_file_differences(mirror, monkeypatch, capsys):
    _commit(mirror, "reference/main", {"a.py": "1\n"}, "feat: add a (rh-01)")
    _commit(mirror, "main", {"a.py": "1\n"}, "feat: add a (rh-01)")

    code, report = _report(capsys, monkeypatch, mirror, {"stories": {"rh-01": _story()}}, [])

    story = report["stories"]["rh-01"]
    assert code == 0
    assert story["merged"] is True
    assert story["reference_merged"] is True
    assert story["files_only_rehearsal"] == []
    assert story["files_only_reference"] == []
    assert report["summary"]["merged_in_both"] == ["rh-01"]


def test_story_merged_only_in_reference_is_listed_under_only_reference(mirror, monkeypatch, capsys):
    _commit(mirror, "reference/main", {"b.py": "1\n"}, "feat: add b (rh-02)")

    code, report = _report(
        capsys, monkeypatch, mirror, {"stories": {"rh-02": _story()}}, []
    )

    story = report["stories"]["rh-02"]
    assert code == 0
    assert story["merged"] is False
    assert story["reference_merged"] is True
    assert report["summary"]["only_reference"] == ["rh-02"]
    assert report["summary"]["merged_in_both"] == []


def test_extra_file_in_rehearsal_commit_is_listed_as_only_rehearsal(mirror, monkeypatch, capsys):
    _commit(mirror, "reference/main", {"c.py": "1\n"}, "feat: add c (rh-03)")
    _commit(mirror, "main", {"c.py": "1\n", "extra.py": "2\n"}, "feat: add c (rh-03)")

    code, report = _report(
        capsys, monkeypatch, mirror, {"stories": {"rh-03": _story()}}, []
    )

    story = report["stories"]["rh-03"]
    assert code == 0
    assert story["files_only_rehearsal"] == ["extra.py"]
    assert story["files_only_reference"] == []


def test_file_only_in_reference_commit_is_listed_as_only_reference(mirror, monkeypatch, capsys):
    _commit(mirror, "reference/main", {"d.py": "1\n", "docs.md": "x\n"}, "feat: d (rh-05)")
    _commit(mirror, "main", {"d.py": "1\n"}, "feat: d (rh-05)")

    code, report = _report(
        capsys, monkeypatch, mirror, {"stories": {"rh-05": _story()}}, []
    )

    assert code == 0
    assert report["stories"]["rh-05"]["files_only_reference"] == ["docs.md"]
    assert report["stories"]["rh-05"]["files_only_rehearsal"] == []


def test_full_mode_execution_is_listed_and_shadow_execution_is_not(mirror, monkeypatch, capsys):
    _commit(mirror, "main", {"e.py": "1\n"}, "feat: e (rh-04)")
    decisions = [
        {
            "story_key": "rh-04",
            "question": "failure triage execution",
            "action": "rebrief",
            "result": "done",
            "mode": "full",
        },
        {
            "story_key": "rh-04",
            "question": "failure triage execution",
            "action": "retry_transient",
            "result": "would run",
            "mode": "shadow",
        },
        {
            "story_key": "rh-04",
            "question": "failure triage execution",
            "action": "park_for_human",
            "result": "dry",
            "mode": "dry-run",
        },
    ]

    code, report = _report(
        capsys, monkeypatch, mirror, {"stories": {"rh-04": _story()}}, decisions
    )

    story = report["stories"]["rh-04"]
    assert code == 0
    assert story["executed_actions"] == [{"action": "rebrief", "result": "done", "mode": "full"}]
    assert report["summary"]["executed_action_counts"] == {"rebrief": 1}
    assert report["summary"]["executed_actions"] == [
        {"story_key": "rh-04", "action": "rebrief", "result": "done", "mode": "full"}
    ]


def test_story_key_prefix_matches_only_its_own_commit(mirror, monkeypatch, capsys):
    _commit(mirror, "main", {"one.py": "1\n"}, "feat: one (rh-1)")
    _commit(mirror, "main", {"ten.py": "1\n"}, "feat: ten (rh-10)")

    code, report = _report(
        capsys,
        monkeypatch,
        mirror,
        {"stories": {"rh-1": _story(), "rh-10": _story()}},
        [],
    )

    assert code == 0
    assert report["stories"]["rh-1"]["merged"] is True
    assert report["stories"]["rh-1"]["files_rehearsal"] == ["one.py"]
    assert report["stories"]["rh-10"]["files_rehearsal"] == ["ten.py"]


def test_repo_that_is_not_a_git_root_exits_1_without_echoing_the_path(
    mirror, monkeypatch, capsys
):
    subdir = mirror / "sub"
    subdir.mkdir()
    monkeypatch.setattr(rr, "_store", _FakeStore({"stories": {}}, []))

    code = rr.main(
        ["--plan", PLAN, "--repo", str(subdir), "--reference", "reference/main"]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert str(subdir) not in captured.err
    assert str(mirror) not in captured.err
    assert captured.out == ""


def test_relative_repo_path_is_refused(mirror, monkeypatch, capsys):
    monkeypatch.setattr(rr, "_store", _FakeStore({"stories": {}}, []))

    code = rr.main(["--plan", PLAN, "--repo", "relative/path", "--reference", "reference/main"])

    assert code == 1
    assert "relative/path" not in capsys.readouterr().err


def test_reference_that_looks_like_an_option_is_refused(mirror, monkeypatch, capsys):
    monkeypatch.setattr(rr, "_store", _FakeStore({"stories": {}}, []))

    code = rr.main(["--plan", PLAN, "--repo", str(mirror), "--reference=--upload-pack=x"])

    assert code == 1


def test_invalid_plan_name_exits_1(mirror, monkeypatch, capsys):
    monkeypatch.setattr(rr, "_store", _FakeStore({"stories": {}}, []))

    code = rr.main(["--plan", "../etc", "--repo", str(mirror), "--reference", "reference/main"])

    assert code == 1
    assert "error: invalid plan name" in capsys.readouterr().err


def test_missing_reference_branch_reports_git_unavailable(mirror, monkeypatch, capsys):
    _commit(mirror, "main", {"f.py": "1\n"}, "feat: f (rh-06)")
    monkeypatch.setattr(rr, "_store", _FakeStore({"stories": {"rh-06": _story()}}, []))

    code = rr.main(
        ["--plan", PLAN, "--repo", str(mirror), "--reference", "no/such-branch", "--json"]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["stories"]["rh-06"]["git"] == "unavailable"
    assert report["stories"]["rh-06"]["merged"] is None


def test_status_tests_and_held_merge_fields_come_from_the_manifest(mirror, monkeypatch, capsys):
    _commit(mirror, "main", {"g.py": "1\n"}, "feat: g (rh-07)")
    manifest = {
        "stories": {
            "rh-07": _story(
                status="parked",
                parked_reason="rebase conflict",
                last_test_check={"returncode": 3},
                pr_url="https://example.invalid/pull/7",
            )
        }
    }
    decisions = [
        {
            "story_key": "rh-07",
            "question": "failure triage",
            "action": "park_for_human",
            "park_class": "rebase_conflict",
            "decided_at": "2026-11-20T10:00:00+00:00",
        }
    ]

    code, report = _report(capsys, monkeypatch, mirror, manifest, decisions)

    story = report["stories"]["rh-07"]
    assert code == 0
    assert story["status"] == "parked"
    assert story["parked_reason"] == "rebase conflict"
    assert story["tests_at_merge"] == 3
    assert story["human_hold"] is True
    assert story["rulings"] == [
        {
            "question": "failure triage",
            "action": "park_for_human",
            "ruling": None,
            "park_class": "rebase_conflict",
            "decided_at": "2026-11-20T10:00:00+00:00",
        }
    ]


def test_held_high_risk_merge_sets_human_hold(mirror, monkeypatch, capsys):
    _commit(mirror, "main", {"h.py": "1\n"}, "feat: h (rh-08)")
    decisions = [
        {
            "story_key": "rh-08",
            "question": "high-risk merge adjudication",
            "ruling": "park",
            "decided_at": "2026-11-20T11:00:00+00:00",
        }
    ]

    code, report = _report(
        capsys, monkeypatch, mirror, {"stories": {"rh-08": _story()}}, decisions
    )

    assert code == 0
    assert report["stories"]["rh-08"]["human_hold"] is True


def test_proceed_ruling_without_park_does_not_set_human_hold(mirror, monkeypatch, capsys):
    _commit(mirror, "main", {"i.py": "1\n"}, "feat: i (rh-09)")
    decisions = [
        {
            "story_key": "rh-09",
            "question": "failure triage",
            "action": "rebrief",
            "park_class": "ci_rework",
            "decided_at": "2026-11-20T12:00:00+00:00",
        }
    ]

    code, report = _report(
        capsys, monkeypatch, mirror, {"stories": {"rh-09": _story()}}, decisions
    )

    assert code == 0
    assert report["stories"]["rh-09"]["human_hold"] is False


def test_report_is_read_only_and_leaves_git_refs_unchanged(mirror, monkeypatch, capsys):
    _commit(mirror, "reference/main", {"j.py": "1\n"}, "feat: j (rh-10)")
    _commit(mirror, "main", {"j.py": "1\n"}, "feat: j (rh-10)")
    refs_before = _git(mirror, "for-each-ref", "--format=%(refname) %(objectname)")
    monkeypatch.setattr(
        rr,
        "_store",
        _FakeStore({"stories": {"rh-10": _story()}}, []),
    )

    code = rr.main(
        ["--plan", PLAN, "--repo", str(mirror), "--reference", "reference/main"]
    )

    assert code == 0
    assert _git(mirror, "for-each-ref", "--format=%(refname) %(objectname)") == refs_before
    assert "rh-10" in capsys.readouterr().out


def test_table_output_does_not_echo_the_repo_path(mirror, monkeypatch, capsys):
    _commit(mirror, "main", {"k.py": "1\n"}, "feat: k (rh-11)")
    monkeypatch.setattr(rr, "_store", _FakeStore({"stories": {"rh-11": _story()}}, []))

    code = rr.main(["--plan", PLAN, "--repo", str(mirror), "--reference", "reference/main"])

    out = capsys.readouterr().out
    assert code == 0
    assert "rh-11" in out
    assert "summary" in out


def test_operator_guide_names_the_rehearsal_settings_and_teardown():
    guide = (REPO_ROOT / "docs" / "guides" / "OVERLORD_REHEARSAL.md").read_text()

    assert "PIPELINE_TRIAGE_MODE=execute" in guide
    assert "-p fagan-rehearsal" in guide
    assert "down -v" in guide


def test_reference_names_the_rehearsal_guide_and_report_command():
    reference = (REPO_ROOT / "REFERENCE.md").read_text()

    assert "docs/guides/OVERLORD_REHEARSAL.md" in reference
    assert "python -m pipeline.rehearsal_report" in reference
