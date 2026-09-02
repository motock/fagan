"""Tests for the side-effecting guard-liveness runner (subprocess story).

Covers the runner additions to ``pipeline/guard_liveness.py``:

- :func:`collect_test_files` -- spawns exactly ONE ``pytest --collect-only``
  subprocess (via :func:`subprocess.run`) with ``cwd=repo_root`` and a
  timeout (default 120s, override via parameter), parses collected test
  file paths from stdout, and returns ``None`` (logging a warning, never
  raising) when the subprocess fails or times out.
- :func:`run_liveness_check` -- loads the dataset JSON (clear
  ``ValueError`` when missing/unparseable), wires the collection list into
  the pure :func:`check_guard_liveness`, and frames recurrence alerts.
- the ``__main__`` CLI -- argparse flags, gate exit codes (0 clean / 1
  alerting), ``--no-collect``, and ``collection_ok`` reporting.

Hermeticity: every subprocess runs against a synthetic fixture repo built
under ``tmp_path`` with an explicit cwd.  Nothing in this module reads the
real repo's ``tests/`` or ``docs/`` trees (the only real-repo file read is
``pipeline/guard_liveness.py`` itself, for the size bound).

The subprocess-level tests intercept :func:`subprocess.run` (the runner's
documented mechanism) so no broken interpreter is ever spawned; exactly one
test invokes the real module CLI end-to-end.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from pipeline import guard_liveness
from pipeline.guard_liveness import collect_test_files, main, run_liveness_check

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The exact command the brief pins for the single collection subprocess.
PYTEST_COLLECT_CMD = [
    sys.executable,
    "-m",
    "pytest",
    "--collect-only",
    "-q",
    "--ignore=tests/benchmark",
    "--ignore=tests/experiments",
]

PASSING_TEST_SOURCE = "def test_ok():\n    assert True\n"
DARK_TEST_SOURCE = "def test_dark():\n    assert True\n"

# Realistic `pytest --collect-only -q` stdout: one nodeid line per collected
# test, a blank line, then the summary line.  The trailing path AFTER the
# summary line grades "parse until the summary line" strictly.
COLLECT_STDOUT = (
    "tests/test_ok.py::test_ok\n"
    "tests/test_ok.py::test_ok_again\n"
    "tests/test_dark.py::test_dark\n"
    "\n"
    "2 tests collected in 0.01s\n"
    "tests/after_summary_must_be_ignored.py\n"
)


# --------------------------------------------------------------------------- #
# fixture helpers
# --------------------------------------------------------------------------- #
def _make_fixture_repo(root: Path) -> Path:
    """Build a synthetic repo: tests/ with two real passing test files."""
    repo = root / "fixture_repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "tests" / "conftest.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_ok.py").write_text(PASSING_TEST_SOURCE, encoding="utf-8")
    (repo / "tests" / "test_dark.py").write_text(DARK_TEST_SOURCE, encoding="utf-8")
    return repo


def _write_dataset(repo: Path, entries: list[dict], name: str = "failure_modes.json") -> Path:
    """Write a synthetic dataset (top-level JSON array) into the fixture repo."""
    path = repo / "docs" / name
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return path


def _base_entries() -> list[dict]:
    """Three entries: clean FIXED, FIXED citing a ghost file, NOT fixed."""
    return [
        {"mode": "M-1", "status": "FIXED (with 28)", "guard": "`test_ok.py`"},
        {"mode": "M-2", "status": "FIXED", "guard": "`test_ghost.py`"},
        {"mode": "M-3", "status": "NOT fixed", "guard": "`test_absent.py`"},
    ]


class _FakeRun:
    """Stand-in for subprocess.run recording every call."""

    def __init__(self, stdout: str = "", returncode: int = 0, error: Exception | None = None):
        self.calls: list[tuple[tuple, dict]] = []
        self._stdout = stdout
        self._returncode = returncode
        self._error = error

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._error is not None:
            raise self._error
        return subprocess.CompletedProcess(
            args=[], returncode=self._returncode, stdout=self._stdout, stderr=""
        )


def _patch_subprocess_run(monkeypatch: pytest.MonkeyPatch, fake: _FakeRun) -> None:
    """Patch subprocess.run no matter how the module imported it."""
    monkeypatch.setattr(subprocess, "run", fake)
    if hasattr(guard_liveness, "run"):  # `from subprocess import run` style
        monkeypatch.setattr(guard_liveness, "run", fake, raising=False)


def _main_exit_code(argv: list[str]) -> int:
    """Invoke the CLI main logic as a function and normalize the exit code."""
    try:
        result = main(list(argv))
    except SystemExit as exc:  # tolerate a sys.exit()-style main
        return 0 if exc.code is None else int(exc.code)
    assert isinstance(result, int), (
        f"main(...) must return an int exit code, got {result!r}"
    )
    return result


def _tree_snapshot(repo: Path) -> list[str]:
    return sorted(str(p.relative_to(repo)) for p in repo.rglob("*"))


def _alerts_by_mode(report: dict) -> dict[str, dict]:
    return {alert["mode"]: alert for alert in report["recurrence_alerts"]}


# --------------------------------------------------------------------------- #
# collect_test_files
# --------------------------------------------------------------------------- #
def test_collect_test_files_spawns_exactly_one_pytest_subprocess(tmp_path, monkeypatch):
    repo = _make_fixture_repo(tmp_path)
    fake = _FakeRun(stdout=COLLECT_STDOUT)
    _patch_subprocess_run(monkeypatch, fake)

    result = collect_test_files(repo)

    assert result == ["tests/test_ok.py", "tests/test_dark.py"]
    assert len(fake.calls) == 1, "collect_test_files must run exactly ONE subprocess"
    args, kwargs = fake.calls[0]
    assert list(args[0]) == PYTEST_COLLECT_CMD
    cwd = kwargs.get("cwd", args[1] if len(args) > 1 else None)
    assert cwd is not None and Path(cwd) == repo
    assert kwargs.get("timeout") == 120, "default timeout must be 120s"


def test_collect_test_files_timeout_override(tmp_path, monkeypatch):
    repo = _make_fixture_repo(tmp_path)
    fake = _FakeRun(stdout=COLLECT_STDOUT)
    _patch_subprocess_run(monkeypatch, fake)

    collect_test_files(repo, timeout=7)

    assert fake.calls[0][1].get("timeout") == 7


def test_collect_test_files_parses_until_summary_line_and_dedupes(tmp_path, monkeypatch):
    repo = _make_fixture_repo(tmp_path)
    stdout = (
        "tests/test_a.py::test_one\n"
        "tests/test_a.py::test_two\n"
        "\n"
        "1 test collected in 0.01s\n"
        "tests/after_summary.py::test_never\n"
    )
    fake = _FakeRun(stdout=stdout)
    _patch_subprocess_run(monkeypatch, fake)

    result = collect_test_files(repo)

    assert result == ["tests/test_a.py"], (
        "duplicate nodeids collapse to one file and lines after the summary "
        "line are never parsed"
    )


def test_collect_test_files_empty_collection_yields_empty_list(tmp_path, monkeypatch):
    repo = _make_fixture_repo(tmp_path)
    fake = _FakeRun(stdout="\nno tests collected in 0.00s\n")
    _patch_subprocess_run(monkeypatch, fake)

    assert collect_test_files(repo) == []


def test_collect_test_files_returns_none_and_warns_on_failure(tmp_path, monkeypatch, caplog):
    repo = _make_fixture_repo(tmp_path)

    raising = _FakeRun(error=subprocess.CalledProcessError(2, PYTEST_COLLECT_CMD))
    _patch_subprocess_run(monkeypatch, raising)
    with caplog.at_level(logging.WARNING):
        assert collect_test_files(repo) is None
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records), (
        "a subprocess failure must log a warning"
    )

    caplog.clear()
    nonzero = _FakeRun(stdout="", returncode=2)
    _patch_subprocess_run(monkeypatch, nonzero)
    with caplog.at_level(logging.WARNING):
        assert collect_test_files(repo) is None
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)


def test_collect_test_files_returns_none_and_warns_on_timeout(tmp_path, monkeypatch, caplog):
    repo = _make_fixture_repo(tmp_path)
    fake = _FakeRun(error=subprocess.TimeoutExpired(cmd=PYTEST_COLLECT_CMD, timeout=120))
    _patch_subprocess_run(monkeypatch, fake)

    with caplog.at_level(logging.WARNING):
        assert collect_test_files(repo) is None
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)


def test_collect_test_files_never_raises_on_unusable_cwd(tmp_path, caplog):
    # cwd points at a directory that does not exist: subprocess.run itself
    # raises OSError before pytest even starts.  Must still return None.
    with caplog.at_level(logging.WARNING):
        assert collect_test_files(tmp_path / "no_such_repo") is None
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)


def test_collect_test_files_real_subprocess_collects_fixture_tests(tmp_path):
    repo = _make_fixture_repo(tmp_path)

    result = collect_test_files(repo)

    assert isinstance(result, list) and result, "fixture has two collectable tests"
    for entry in result:
        assert entry.endswith(".py"), f"collected entries are file paths: {entry!r}"
        assert "::" not in entry, f"nodeid suffixes must be stripped: {entry!r}"
    basenames = {PurePosixPath(entry.replace("\\", "/")).name for entry in result}
    assert {"test_ok.py", "test_dark.py"} <= basenames


# --------------------------------------------------------------------------- #
# run_liveness_check
# --------------------------------------------------------------------------- #
def test_run_liveness_check_happy_path_with_real_collection(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    dataset = _write_dataset(repo, _base_entries())

    report = run_liveness_check(repo, dataset)

    assert report["summary"]["collection_ok"] is True
    assert report["summary"]["recurrence_alerts"] == len(report["recurrence_alerts"]) == 1
    alert = report["recurrence_alerts"][0]
    assert set(alert) == {"mode", "reason", "files"}
    assert alert == {"mode": "M-2", "reason": "missing_guard", "files": ["test_ghost.py"]}
    entries = {entry["mode"]: entry for entry in report["entries"]}
    assert set(entries) == {"M-1", "M-2", "M-3"}
    assert [entries[m]["expected_live"] for m in ("M-1", "M-2", "M-3")] == [True, True, False]
    assert entries["M-2"]["missing"] == ["test_ghost.py"]
    assert all(entry["uncollected"] == [] for entry in report["entries"])
    # M-3 is missing its cited file but is NOT expected_live: no alert.
    assert entries["M-3"]["missing"] == ["test_absent.py"]
    assert "M-3" not in _alerts_by_mode(report)


def test_run_liveness_check_uncollected_alert_when_collection_omits_file(tmp_path, monkeypatch):
    repo = _make_fixture_repo(tmp_path)
    dataset = _write_dataset(repo, _base_entries())
    monkeypatch.setattr(guard_liveness, "collect_test_files", lambda repo_root, **kw: [])

    report = run_liveness_check(repo, dataset)

    assert report["summary"]["collection_ok"] is True
    alerts = _alerts_by_mode(report)
    assert set(alerts) == {"M-1", "M-2"}
    assert alerts["M-1"]["reason"] == "uncollected_guard"
    assert "test_ok.py" in alerts["M-1"]["files"]
    assert alerts["M-2"]["reason"] == "missing_guard"
    assert alerts["M-2"]["files"] == ["test_ghost.py"]
    assert report["summary"]["recurrence_alerts"] == 2


def test_run_liveness_check_missing_takes_precedence_over_uncollected(tmp_path, monkeypatch):
    repo = _make_fixture_repo(tmp_path)
    entries = [
        {
            "mode": "M-both",
            "status": "FIXED",
            "guard": "`test_ghost.py` + `test_dark.py`",
        }
    ]
    dataset = _write_dataset(repo, entries)
    monkeypatch.setattr(guard_liveness, "collect_test_files", lambda repo_root, **kw: [])

    report = run_liveness_check(repo, dataset)

    assert len(report["recurrence_alerts"]) == 1, "one alert per qualifying entry"
    alert = report["recurrence_alerts"][0]
    assert alert["mode"] == "M-both"
    assert alert["reason"] == "missing_guard"
    assert alert["files"] == ["test_ghost.py"]


def test_run_liveness_check_collection_failure_keeps_uncollected_empty(tmp_path, monkeypatch):
    repo = _make_fixture_repo(tmp_path)
    entries = _base_entries() + [
        {"mode": "M-4", "status": "FIXED", "guard": "none identified"}
    ]
    dataset = _write_dataset(repo, entries)
    monkeypatch.setattr(guard_liveness, "collect_test_files", lambda repo_root, **kw: None)

    report = run_liveness_check(repo, dataset)

    assert report["summary"]["collection_ok"] is False
    assert all(entry["uncollected"] == [] for entry in report["entries"])
    assert [alert["mode"] for alert in report["recurrence_alerts"]] == ["M-2"]
    assert report["recurrence_alerts"][0]["reason"] == "missing_guard"
    assert report["summary"]["recurrence_alerts"] == 1


def test_run_liveness_check_missing_dataset_file_raises_valueerror(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    missing = repo / "docs" / "does_not_exist.json"

    with pytest.raises(ValueError) as excinfo:
        run_liveness_check(repo, missing)
    assert "does_not_exist.json" in str(excinfo.value)


def test_run_liveness_check_malformed_dataset_raises_valueerror(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    bad = repo / "docs" / "broken.json"
    bad.write_text("{ this is not json", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        run_liveness_check(repo, bad)
    assert "broken.json" in str(excinfo.value)


def test_run_liveness_check_emit_true_prints_report_json(tmp_path, monkeypatch, capsys):
    repo = _make_fixture_repo(tmp_path)
    dataset = _write_dataset(repo, _base_entries())
    monkeypatch.setattr(
        guard_liveness, "collect_test_files", lambda repo_root, **kw: ["tests/test_ok.py"]
    )

    report = run_liveness_check(repo, dataset, emit=True)

    captured = capsys.readouterr()
    assert json.loads(captured.out) == report


def test_run_liveness_check_emit_false_prints_nothing(tmp_path, monkeypatch, capsys):
    repo = _make_fixture_repo(tmp_path)
    dataset = _write_dataset(repo, _base_entries())
    monkeypatch.setattr(
        guard_liveness, "collect_test_files", lambda repo_root, **kw: ["tests/test_ok.py"]
    )

    run_liveness_check(repo, dataset, emit=False)

    assert capsys.readouterr().out == ""


def test_run_liveness_check_writes_no_files_into_the_repo(tmp_path, monkeypatch, capsys):
    repo = _make_fixture_repo(tmp_path)
    dataset = _write_dataset(repo, _base_entries())
    monkeypatch.setattr(guard_liveness, "collect_test_files", lambda repo_root, **kw: [])
    before = _tree_snapshot(repo)

    run_liveness_check(repo, dataset, emit=True)
    capsys.readouterr()

    assert _tree_snapshot(repo) == before, "the runner must not write any file"


# --------------------------------------------------------------------------- #
# CLI (__main__) gate behaviour, invoked as a function
# --------------------------------------------------------------------------- #
def test_cli_exits_one_when_recurrence_alerts_nonempty(tmp_path, monkeypatch):
    repo = _make_fixture_repo(tmp_path)
    dataset = _write_dataset(repo, _base_entries())
    monkeypatch.setattr(
        guard_liveness, "collect_test_files", lambda repo_root, **kw: ["tests/test_ok.py"]
    )

    assert _main_exit_code(["--repo-root", str(repo), "--dataset", str(dataset)]) == 1


def test_cli_exits_zero_when_recurrence_alerts_empty(tmp_path, monkeypatch):
    repo = _make_fixture_repo(tmp_path)
    dataset = _write_dataset(repo, [_base_entries()[0]])  # only the clean entry
    monkeypatch.setattr(
        guard_liveness, "collect_test_files", lambda repo_root, **kw: ["tests/test_ok.py"]
    )

    assert _main_exit_code(["--repo-root", str(repo), "--dataset", str(dataset)]) == 0


def test_cli_exits_zero_when_collection_fails(tmp_path, monkeypatch):
    repo = _make_fixture_repo(tmp_path)
    dataset = _write_dataset(repo, [_base_entries()[0]])  # clean entry, no missing
    monkeypatch.setattr(guard_liveness, "collect_test_files", lambda repo_root, **kw: None)

    assert _main_exit_code(["--repo-root", str(repo), "--dataset", str(dataset)]) == 0


def test_cli_no_collect_skips_subprocess_but_still_gates_on_existence(tmp_path, monkeypatch):
    repo = _make_fixture_repo(tmp_path)
    dataset = _write_dataset(repo, _base_entries())
    calls: list[tuple] = []

    def _must_not_run(repo_root, *args, **kwargs):
        calls.append((repo_root, args, kwargs))
        return calls and None  # mirrors a failed collection (None)

    monkeypatch.setattr(guard_liveness, "collect_test_files", _must_not_run)

    exit_code = _main_exit_code(
        ["--repo-root", str(repo), "--dataset", str(dataset), "--no-collect"]
    )

    assert calls == [], "--no-collect must skip the pytest subprocess entirely"
    assert exit_code == 1, "existence-only checking still flags the missing guard"


def test_cli_dataset_defaults_to_docs_failure_modes_json(tmp_path, monkeypatch):
    repo = _make_fixture_repo(tmp_path)
    _write_dataset(repo, _base_entries())  # default location, has one alert
    monkeypatch.setattr(
        guard_liveness, "collect_test_files", lambda repo_root, **kw: ["tests/test_ok.py"]
    )

    assert _main_exit_code(["--repo-root", str(repo)]) == 1


def test_cli_repo_root_defaults_to_cwd(tmp_path, monkeypatch):
    repo = _make_fixture_repo(tmp_path)
    _write_dataset(repo, _base_entries())
    monkeypatch.setattr(
        guard_liveness, "collect_test_files", lambda repo_root, **kw: ["tests/test_ok.py"]
    )
    monkeypatch.chdir(repo)

    assert _main_exit_code([]) == 1


# --------------------------------------------------------------------------- #
# the single end-to-end subprocess invocation of the module CLI
# --------------------------------------------------------------------------- #
def test_module_cli_subprocess_exit_code_and_json_stdout(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    dataset = _write_dataset(repo, _base_entries())
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [sys.executable, "-m", "pipeline.guard_liveness",
         "--repo-root", str(repo), "--dataset", str(dataset)],
        cwd=str(repo),  # explicit cwd: hermetic, never the real repo
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert proc.returncode == 1, f"gate must exit 1 with an alert; stderr={proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["summary"]["recurrence_alerts"] == len(payload["recurrence_alerts"]) == 1
    assert payload["recurrence_alerts"][0]["reason"] == "missing_guard"
    assert payload["summary"]["collection_ok"] is True


# --------------------------------------------------------------------------- #
# module bound
# --------------------------------------------------------------------------- #
def test_guard_liveness_module_stays_under_1000_lines():
    source = (PROJECT_ROOT / "pipeline" / "guard_liveness.py").read_text(encoding="utf-8")
    line_count = len(source.splitlines())
    assert line_count < 1000, f"pipeline/guard_liveness.py must stay under 1000 lines, got {line_count}"