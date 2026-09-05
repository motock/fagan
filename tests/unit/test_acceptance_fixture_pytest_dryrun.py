"""Acceptance oracle: lint (_lint_acceptance_fixtures) catches lint hygiene -
the actual 2026-08-05 PR #235 failure - but pytest collection alone was
historically treated as sufficient and was not. _pytest_acceptance_fixtures
runs `pytest --collect-only` against a story's materialized acceptance
fixtures so a fixture that is lint-clean yet fails to *collect* (a
module-level crash, an undefined import outside the repo package) is caught
before it becomes a live, read-only oracle. This module tests the validator
itself; wiring it into plan-ingest is left to a follow-up story, mirroring
_lint_acceptance_fixtures.
"""
import contextlib
import os
import shutil
import subprocess

import pytest

from pipeline.build_detect import _pytest_acceptance_fixtures

CLEAN_SOURCE = "def test_x():\n    assert True\n"
BOOM_SOURCE = 'raise ValueError("boom at collection")\n'


def _story(entries, summary="s"):
    return {"summary": summary, "acceptance": entries}


def _fake_temporary_directory(tmp_path):
    """Build a stand-in for ``tempfile.TemporaryDirectory`` whose ``__enter__``
    yields ``str(tmp_path)`` so tests can inspect materialized fixture files
    without waiting on the real context manager's cleanup.
    """

    @contextlib.contextmanager
    def factory(*a, **k):
        yield str(tmp_path)

    return factory


def _install_fake_pytest(monkeypatch, tmp_path, returncode=0, output=""):
    """Stub shutil.which + subprocess.run + tempfile.TemporaryDirectory, and
    return the dict that fake_run populates with the captured cmd/kwargs.
    """
    monkeypatch.setattr(
        "pipeline.build_detect.shutil.which", lambda name: "/usr/bin/pytest"
    )
    monkeypatch.setattr(
        "pipeline.build_detect.tempfile.TemporaryDirectory",
        _fake_temporary_directory(tmp_path),
    )
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            cmd, returncode=returncode, stdout=output, stderr=""
        )

    monkeypatch.setattr("pipeline.build_detect.subprocess.run", fake_run)
    return captured


# --- happy path -------------------------------------------------------


def test_clean_fixture_returns_clean_and_uses_collect_only_quiet(
    monkeypatch, tmp_path
):
    captured = _install_fake_pytest(monkeypatch, tmp_path, returncode=0)

    story = _story([{"path": "tests/unit/test_x.py", "source": CLEAN_SOURCE}])
    kind, msg = _pytest_acceptance_fixtures(story)

    assert (kind, msg) == ("clean", None)
    assert "--collect-only" in captured["cmd"]
    assert "-q" in captured["cmd"]


def test_materializes_fixture_source_into_fresh_mkdtemp_dir(monkeypatch, tmp_path):
    captured = _install_fake_pytest(monkeypatch, tmp_path, returncode=0)

    story = _story([{"path": "tests/unit/test_x.py", "source": CLEAN_SOURCE}])
    kind, msg = _pytest_acceptance_fixtures(story)

    assert (kind, msg) == ("clean", None)
    assert captured["cmd"][-1] == str(tmp_path)
    assert (tmp_path / "tests" / "unit" / "test_x.py").read_text() == CLEAN_SOURCE


def test_subprocess_invoked_with_full_command_and_kwargs(monkeypatch, tmp_path):
    captured = _install_fake_pytest(monkeypatch, tmp_path, returncode=0)
    monkeypatch.setenv("PYTHONPATH", "/existing/path")

    story = _story([{"path": "tests/unit/test_x.py", "source": CLEAN_SOURCE}])
    kind, _msg = _pytest_acceptance_fixtures(story, repo_root="/repo/root")

    assert kind == "clean"
    assert captured["cmd"] == [
        "/usr/bin/pytest",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        str(tmp_path),
    ]
    kwargs = captured["kwargs"]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 120
    assert kwargs["cwd"] == "/repo/root"
    assert kwargs["env"]["PYTHONPATH"] == "/repo/root" + os.pathsep + "/existing/path"


def test_cwd_defaults_to_tmp_dir_when_repo_root_omitted(monkeypatch, tmp_path):
    captured = _install_fake_pytest(monkeypatch, tmp_path, returncode=0)

    story = _story([{"path": "tests/unit/test_x.py", "source": CLEAN_SOURCE}])
    _pytest_acceptance_fixtures(story)

    assert captured["kwargs"]["cwd"] == str(tmp_path)


def test_pythonpath_created_when_absent_and_repo_root_given(monkeypatch, tmp_path):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    captured = _install_fake_pytest(monkeypatch, tmp_path, returncode=0)

    story = _story([{"path": "tests/unit/test_x.py", "source": CLEAN_SOURCE}])
    _pytest_acceptance_fixtures(story, repo_root="/repo/root")

    assert captured["kwargs"]["env"]["PYTHONPATH"] == "/repo/root"


def test_pythonpath_untouched_when_repo_root_omitted(monkeypatch, tmp_path):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    captured = _install_fake_pytest(monkeypatch, tmp_path, returncode=0)

    story = _story([{"path": "tests/unit/test_x.py", "source": CLEAN_SOURCE}])
    _pytest_acceptance_fixtures(story)

    assert "PYTHONPATH" not in captured["kwargs"]["env"]


# --- collection failures ------------------------------------------------


def test_generic_collection_error_returns_finding(monkeypatch, tmp_path):
    output = "SyntaxError: invalid syntax\ntests/unit/test_x.py:1"
    _install_fake_pytest(monkeypatch, tmp_path, returncode=2, output=output)

    story = _story(
        [{"path": "tests/unit/test_x.py", "source": "def(\n"}], summary="my-story"
    )
    kind, msg = _pytest_acceptance_fixtures(story)

    assert kind == "finding"
    assert "tests/unit/test_x.py" in msg
    assert "my-story" in msg


def test_repo_package_modulenotfound_returns_skipped_with_exact_message(
    monkeypatch, tmp_path
):
    output = "ModuleNotFoundError: No module named 'pipeline.thing'"
    _install_fake_pytest(monkeypatch, tmp_path, returncode=2, output=output)

    story = _story([{"path": "tests/unit/test_x.py", "source": CLEAN_SOURCE}])
    kind, msg = _pytest_acceptance_fixtures(story)

    assert kind == "skipped"
    assert msg == (
        "acceptance-fixture pytest dry-run skipped: fixture imports repo code "
        "that does not exist until the story is implemented - cannot validate "
        "collection at ingest time"
    )


def test_repo_package_importerror_returns_skipped(monkeypatch, tmp_path):
    output = "ImportError: cannot import name 'foo' from 'pipeline.bar'"
    _install_fake_pytest(monkeypatch, tmp_path, returncode=2, output=output)

    story = _story([{"path": "tests/unit/test_x.py", "source": CLEAN_SOURCE}])
    kind, msg = _pytest_acceptance_fixtures(story)

    assert kind == "skipped"
    assert msg is not None


def test_unrelated_package_modulenotfound_returns_finding(monkeypatch, tmp_path):
    output = "ModuleNotFoundError: No module named 'some_other_pkg'"
    _install_fake_pytest(monkeypatch, tmp_path, returncode=2, output=output)

    story = _story(
        [{"path": "tests/unit/test_x.py", "source": CLEAN_SOURCE}], summary="my-story"
    )
    kind, msg = _pytest_acceptance_fixtures(story)

    assert kind == "finding"
    assert "my-story" in msg


def test_pipeline_substring_without_import_keyword_is_finding(monkeypatch, tmp_path):
    output = "SyntaxError: invalid syntax in pipeline/thing.py fixture"
    _install_fake_pytest(monkeypatch, tmp_path, returncode=2, output=output)

    story = _story([{"path": "tests/unit/test_x.py", "source": CLEAN_SOURCE}])
    kind, _msg = _pytest_acceptance_fixtures(story)

    assert kind == "finding"


# --- no-op boundary cases -------------------------------------------------


def test_no_acceptance_key_returns_clean_and_skips_subprocess(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "pipeline.build_detect.subprocess.run", lambda *a, **k: calls.append(1)
    )
    monkeypatch.setattr(
        "pipeline.build_detect.tempfile.TemporaryDirectory",
        lambda *a, **k: calls.append(1),
    )

    kind, msg = _pytest_acceptance_fixtures({"summary": "s"})

    assert (kind, msg) == ("clean", None)
    assert calls == []


def test_empty_acceptance_list_returns_clean_and_skips_subprocess(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "pipeline.build_detect.subprocess.run", lambda *a, **k: calls.append(1)
    )

    kind, msg = _pytest_acceptance_fixtures(_story([]))

    assert (kind, msg) == ("clean", None)
    assert calls == []


def test_no_py_entries_returns_clean_and_skips_subprocess(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "pipeline.build_detect.subprocess.run", lambda *a, **k: calls.append(1)
    )

    story = _story([{"path": "docs/notes.md", "source": "# notes\n"}])
    kind, msg = _pytest_acceptance_fixtures(story)

    assert (kind, msg) == ("clean", None)
    assert calls == []


def test_missing_pytest_on_path_returns_skipped_with_exact_message(monkeypatch):
    calls = []
    monkeypatch.setattr("pipeline.build_detect.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "pipeline.build_detect.subprocess.run", lambda *a, **k: calls.append(1)
    )

    story = _story([{"path": "tests/unit/test_x.py", "source": CLEAN_SOURCE}])
    kind, msg = _pytest_acceptance_fixtures(story)

    assert kind == "skipped"
    assert msg == "acceptance-fixture pytest dry-run skipped: pytest not found on PATH"
    assert calls == []


def test_subprocess_exception_returns_skipped_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "pipeline.build_detect.shutil.which", lambda name: "/usr/bin/pytest"
    )
    monkeypatch.setattr(
        "pipeline.build_detect.tempfile.TemporaryDirectory",
        _fake_temporary_directory(tmp_path),
    )

    def raising_run(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("pipeline.build_detect.subprocess.run", raising_run)

    story = _story([{"path": "tests/unit/test_x.py", "source": CLEAN_SOURCE}])
    kind, msg = _pytest_acceptance_fixtures(story)

    assert kind == "skipped"
    assert msg is not None
    assert msg.startswith("acceptance-fixture pytest dry-run skipped:")


def test_subprocess_timeout_returns_skipped_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "pipeline.build_detect.shutil.which", lambda name: "/usr/bin/pytest"
    )
    monkeypatch.setattr(
        "pipeline.build_detect.tempfile.TemporaryDirectory",
        _fake_temporary_directory(tmp_path),
    )

    def timing_out_run(cmd, **k):
        raise subprocess.TimeoutExpired(cmd, 120)

    monkeypatch.setattr("pipeline.build_detect.subprocess.run", timing_out_run)

    story = _story([{"path": "tests/unit/test_x.py", "source": CLEAN_SOURCE}])
    kind, msg = _pytest_acceptance_fixtures(story)

    assert kind == "skipped"
    assert msg is not None
    assert msg.startswith("acceptance-fixture pytest dry-run skipped:")


# --- docstring rationale ---------------------------------------------------


def test_docstring_explains_collection_vs_lint_rationale():
    doc = _pytest_acceptance_fixtures.__doc__ or ""
    assert "collect" in doc.lower()
    assert "lint" in doc.lower()


# --- real-tool integration -------------------------------------------------


@pytest.mark.skipif(shutil.which("pytest") is None, reason="pytest not on PATH")
def test_real_pytest_flags_module_level_crash_as_finding():
    story = _story(
        [{"path": "tests/unit/test_boom_at_collection.py", "source": BOOM_SOURCE}],
        summary="boom-story",
    )
    kind, msg = _pytest_acceptance_fixtures(story)

    assert kind == "finding"
    assert "boom-story" in msg


@pytest.mark.skipif(shutil.which("pytest") is None, reason="pytest not on PATH")
def test_real_pytest_clean_fixture_returns_clean():
    story = _story([{"path": "tests/unit/test_clean_real.py", "source": CLEAN_SOURCE}])
    kind, msg = _pytest_acceptance_fixtures(story)

    assert (kind, msg) == ("clean", None)
