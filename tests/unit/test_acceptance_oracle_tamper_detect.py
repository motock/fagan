"""Acceptance oracle: the merge-gate acceptance reverify must refuse a worktree
whose acceptance fixture no longer matches the digest recorded at dispatch.

This is the hole PR #210 merged through: _reverify_acceptance runs whatever
fixture file is in the worktree, so rewriting the grader makes the gate pass.
The refusal must happen WITHOUT running the (rewritten) tests at all.
"""
import hashlib

from pipeline import ci

SOURCE = "def test_x():\n    assert True\n"
DIGEST = hashlib.sha256(SOURCE.encode()).hexdigest()


def _story():
    return {
        "acceptance": [{"path": "oracle.py", "source": SOURCE}],
        "acceptance_digests": {"oracle.py": DIGEST},
    }


def test_untouched_fixture_is_not_reported(tmp_path):
    (tmp_path / "oracle.py").write_text(SOURCE)
    assert ci._acceptance_tampered(_story(), str(tmp_path)) == []


def test_modified_fixture_is_reported(tmp_path):
    (tmp_path / "oracle.py").write_text(SOURCE + "# sneaky\n")
    assert ci._acceptance_tampered(_story(), str(tmp_path)) == ["oracle.py"]


def test_deleted_fixture_is_reported(tmp_path):
    assert ci._acceptance_tampered(_story(), str(tmp_path)) == ["oracle.py"]


def test_story_without_digests_reports_nothing(tmp_path):
    assert ci._acceptance_tampered({"acceptance": []}, str(tmp_path)) == []


def test_reverify_fails_a_tampered_worktree_without_running_tests(tmp_path, monkeypatch):
    (tmp_path / "oracle.py").write_text(SOURCE + "# sneaky\n")

    def boom(*a, **k):
        raise AssertionError(
            "the rewritten oracle must never be executed - the gate must refuse first"
        )

    monkeypatch.setattr(ci.subprocess, "run", boom)
    result = ci._reverify_acceptance(_story(), str(tmp_path))
    assert result["state"] == "fail"
    assert "oracle.py" in result["error"]


def test_reverify_still_runs_tests_for_an_untouched_worktree(tmp_path, monkeypatch):
    (tmp_path / "oracle.py").write_text(SOURCE)
    called = {}

    class R:
        returncode = 0
        stdout = "1 passed"
        stderr = ""

    def fake_run(cmd, **kwargs):
        called["ran"] = True
        return R()

    monkeypatch.setattr(ci.subprocess, "run", fake_run)
    result = ci._reverify_acceptance(_story(), str(tmp_path))
    assert called.get("ran"), "an untouched oracle must still be executed"
    assert result["state"] == "pass"

