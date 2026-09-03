"""Acceptance oracle: a lint-violating fixture is a read-only oracle the
dispatched agent can never fix, so repo-wide ruff fails every rework attempt
with no way out (PR #235, 2026-08-05). _lint_acceptance_fixtures materializes
a story's acceptance fixtures into a scratch dir and runs ruff against them,
so a violation can be caught before the fixture becomes a live oracle. This
module tests the validator itself; wiring it into plan-ingest is groundwork
left to a follow-up story.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline.build_detect import (
    _lint_acceptance_fixtures,
    _materialize_acceptance_fixtures,
)

CLEAN_SOURCE = "def test_x():\n    assert True\n"
F401_SOURCE = "import os\n\n\ndef test_x():\n    assert True\n"


def _story(entries, summary="s"):
    return {"summary": summary, "acceptance": entries}


def test_clean_fixture_returns_clean_and_materializes_source(monkeypatch, tmp_path):
    written = {}

    def fake_which(name):
        return "/usr/bin/ruff"

    def fake_run(cmd, **kwargs):
        # the scratch dir is a TemporaryDirectory, so it only exists for the
        # duration of this call - read the materialized file here, not after
        # _lint_acceptance_fixtures returns and the dir is cleaned up
        written_file = Path(cmd[-1]) / "tests/unit/test_x.py"
        written["source"] = written_file.read_text()
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("pipeline.build_detect.shutil.which", fake_which)
    monkeypatch.setattr("pipeline.build_detect.subprocess.run", fake_run)

    story = _story([{"path": "tests/unit/test_x.py", "source": CLEAN_SOURCE}])
    kind, msg = _lint_acceptance_fixtures(story)

    assert (kind, msg) == ("clean", None)
    assert written["source"] == CLEAN_SOURCE


def test_violation_returns_finding_naming_path_and_ruff(monkeypatch):
    def fake_which(name):
        return "/usr/bin/ruff"

    sample_output = "tests/unit/test_x.py:1:8: F401 [*] `os` imported but unused"

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, returncode=1, stdout=sample_output, stderr=""
        )

    monkeypatch.setattr("pipeline.build_detect.shutil.which", fake_which)
    monkeypatch.setattr("pipeline.build_detect.subprocess.run", fake_run)

    story = _story(
        [{"path": "tests/unit/test_x.py", "source": F401_SOURCE}], summary="my-story"
    )
    kind, msg = _lint_acceptance_fixtures(story)

    assert kind == "finding"
    assert "tests/unit/test_x.py" in msg
    assert "ruff" in msg
    assert "my-story" in msg


def test_story_with_no_acceptance_key_is_clean_and_skips_subprocess(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "pipeline.build_detect.subprocess.run", lambda *a, **k: calls.append(1)
    )

    kind, msg = _lint_acceptance_fixtures({"summary": "s"})

    assert (kind, msg) == ("clean", None)
    assert calls == []


def test_non_python_fixture_is_clean_and_skips_subprocess(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "pipeline.build_detect.subprocess.run", lambda *a, **k: calls.append(1)
    )

    story = _story([{"path": "docs/notes.md", "source": "# notes\n"}])
    kind, msg = _lint_acceptance_fixtures(story)

    assert (kind, msg) == ("clean", None)
    assert calls == []


def test_missing_ruff_on_path_is_skipped_and_skips_subprocess(monkeypatch):
    calls = []
    monkeypatch.setattr("pipeline.build_detect.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "pipeline.build_detect.subprocess.run", lambda *a, **k: calls.append(1)
    )

    story = _story([{"path": "tests/unit/test_x.py", "source": CLEAN_SOURCE}])
    kind, msg = _lint_acceptance_fixtures(story)

    assert kind == "skipped"
    assert msg is not None
    assert calls == []


def test_subprocess_exception_is_skipped_never_raises(monkeypatch):
    monkeypatch.setattr(
        "pipeline.build_detect.shutil.which", lambda name: "/usr/bin/ruff"
    )

    def raising_run(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("pipeline.build_detect.subprocess.run", raising_run)

    story = _story([{"path": "tests/unit/test_x.py", "source": CLEAN_SOURCE}])
    kind, msg = _lint_acceptance_fixtures(story)

    assert kind == "skipped"
    assert msg is not None


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not on PATH")
def test_real_ruff_flags_unused_import():
    story = _story([{"path": "tests/unit/test_x.py", "source": F401_SOURCE}])
    kind, msg = _lint_acceptance_fixtures(story)

    assert kind == "finding"
    assert "tests/unit/test_x.py" in msg


def test_materialize_writes_files_and_returns_paths(tmp_path):
    story = _story(
        [
            {"path": "a/b.py", "source": "x = 1\n"},
            {"path": "c.py", "source": "y = 2\n"},
        ]
    )
    written = _materialize_acceptance_fixtures(story, tmp_path)

    assert len(written) == 2
    assert (tmp_path / "a" / "b.py").read_text() == "x = 1\n"
    assert (tmp_path / "c.py").read_text() == "y = 2\n"


def test_materialize_skips_malformed_entries(tmp_path):
    story = _story(
        [
            {"path": "", "source": "x = 1\n"},
            {"path": "a.py", "source": ""},
            {"path": "b.py"},
            {"source": "z = 1\n"},
        ]
    )
    written = _materialize_acceptance_fixtures(story, tmp_path)

    assert written == []


def test_materialize_with_no_acceptance_key_returns_empty_list(tmp_path):
    assert _materialize_acceptance_fixtures({"summary": "s"}, tmp_path) == []


def test_materialize_skips_absolute_path(tmp_path):
    escape_target = tmp_path.parent / "escaped_absolute.py"
    story = _story([{"path": str(escape_target), "source": "x = 1\n"}])

    written = _materialize_acceptance_fixtures(story, tmp_path)

    assert written == []
    assert not escape_target.exists()


def test_materialize_skips_parent_traversal_path(tmp_path):
    dest_dir = tmp_path / "scratch"
    dest_dir.mkdir()
    escape_target = tmp_path / "escaped_traversal.py"
    story = _story([{"path": "../escaped_traversal.py", "source": "x = 1\n"}])

    written = _materialize_acceptance_fixtures(story, dest_dir)

    assert written == []
    assert not escape_target.exists()
