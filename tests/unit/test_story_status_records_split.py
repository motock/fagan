"""RH-12: the grade-record helpers moved to story_status_records; story_status re-exports them."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import pipeline.story_status as orig
import pipeline.story_status_records as new

MOVED = ("_record_test_check", "_clear_failure_streaks")
REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", MOVED)
def test_should_reexport_the_same_object_from_story_status(name):
    assert getattr(orig, name) is getattr(new, name)


@pytest.mark.parametrize("name", MOVED)
def test_should_import_moved_symbol_from_new_module(name):
    assert callable(getattr(new, name))


@pytest.mark.parametrize("path", ["pipeline/story_status.py", "pipeline/story_status_records.py"])
def test_should_keep_file_under_1000_lines(path):
    assert len((REPO / path).read_text().splitlines()) < 1000


def test_should_reach_subprocess_patch_made_via_story_status(monkeypatch, tmp_path):
    # Patching subprocess.run through the original module's attribute must still
    # change what the moved helper records.
    fake = SimpleNamespace(stdout="deadbeef\n")
    monkeypatch.setattr(orig.subprocess, "run", lambda *a, **k: fake)
    story: dict = {}
    result = SimpleNamespace(returncode=0, stdout="ok", stderr="")

    sha = orig._record_test_check(story, ["pytest"], tmp_path, result, str(tmp_path))

    assert sha == "deadbeef"
    assert story["last_test_check"]["sha"] == "deadbeef"


def test_should_record_none_sha_when_worktree_missing(tmp_path):
    story: dict = {}
    result = SimpleNamespace(returncode=1, stdout=None, stderr=None)

    sha = new._record_test_check(story, ["pytest"], tmp_path, result, str(tmp_path / "nope"))

    assert sha is None
    assert story["last_test_check"]["stdout_tail"] == ""


def test_should_record_none_sha_when_git_fails(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "git")

    monkeypatch.setattr(new.subprocess, "run", boom)
    result = SimpleNamespace(returncode=0, stdout="", stderr="")

    assert new._record_test_check({}, [], tmp_path, result, str(tmp_path)) is None


def test_should_clear_only_failure_streak_keys():
    story = {"dispatch_attempts": 2, "watchdog_streak": 1, "infra_failure_streak_model": "m", "summary": "keep"}

    new._clear_failure_streaks(story)

    assert story == {"summary": "keep"}
