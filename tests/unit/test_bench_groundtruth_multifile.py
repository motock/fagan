"""run_groundtruth must be able to grade multi-file (Tier 3) tasks.

A Tier 3 task's ground truth imports several modules the agent changed,
but run_groundtruth() used to copy only `impl_src / impl_file` beside
test_groundtruth.py, so such a task could never pass. These tests drive
the real public entry points (run_groundtruth, load_task and the
extra_impl_files validator) against a synthetic multi-module tree.
"""

import inspect
import json
import subprocess

import pytest

from tests.benchmark import harness

INIT_PY = 'LABEL = "pkg"\n'
API_PY = "VALUE = 42\n"
REPORT_PY = "def build():\n    return 42\n"
DEEP_PY = "DEEP = 7\n"
GT_BOTH = (
    "from pkg import api\n"
    "from pkg import report\n"
    "from pkg.deep import util\n"
    "\n"
    "\n"
    "def test_report_and_api():\n"
    "    assert report.build() == 42\n"
    "    assert api.VALUE == 42\n"
    "    assert util.DEEP == 7\n"
)
GT_REPORT_ONLY = (
    "from pkg import report\n\n\ndef test_report():\n    assert report.build() == 42\n"
)


def _write_pkg_src(src):
    (src / "pkg" / "deep").mkdir(parents=True)
    (src / "pkg" / "__init__.py").write_text(INIT_PY)
    (src / "pkg" / "api.py").write_text(API_PY)
    (src / "pkg" / "report.py").write_text(REPORT_PY)
    (src / "pkg" / "deep" / "util.py").write_text(DEEP_PY)


def _write_task(tasks, name, spec):
    task_dir = tasks / name
    task_dir.mkdir(parents=True)
    (task_dir / "spec.json").write_text(json.dumps(spec))
    (task_dir / "groundtruth.py").write_text("def test_ok():\n    assert True\n")


# ---------- run_groundtruth: pytest ecosystem ----------


@pytest.mark.parametrize(
    "extras",
    [
        ["pkg/__init__.py", "pkg/api.py", "pkg/deep/util.py"],
        ("pkg/__init__.py", "pkg/api.py", "pkg/deep/util.py"),
    ],
)
def test_run_groundtruth_copies_extra_impl_files(tmp_path, extras):
    """The ground truth imports pkg.api and pkg.deep.util, so every extra
    file must be copied to the same relative path under scratch (list or
    tuple both accepted; nested dirs are created with parents)."""
    src = tmp_path / "src"
    _write_pkg_src(src)
    scratch = tmp_path / "scratch"
    gt = harness.run_groundtruth(
        src, "pkg/report.py", GT_BOTH, scratch, extra_impl_files=extras
    )
    assert gt["ran"] is True
    assert gt["passed"] is True, gt.get("tail", "")
    assert (scratch / "pkg" / "__init__.py").read_text() == INIT_PY
    assert (scratch / "pkg" / "api.py").read_text() == API_PY
    assert (scratch / "pkg" / "deep" / "util.py").read_text() == DEEP_PY
    assert (scratch / "pkg" / "report.py").read_text() == REPORT_PY


def test_run_groundtruth_without_extras_fails(tmp_path):
    """Without the extra copies the same ground truth must fail - proving
    the copies (not luck) are what makes the multi-file task pass. Also
    grades the `extra_impl_files = ()` default: no kwarg, no crash."""
    src = tmp_path / "src"
    _write_pkg_src(src)
    gt = harness.run_groundtruth(src, "pkg/report.py", GT_BOTH, tmp_path / "scratch")
    assert gt["ran"] is True
    assert gt["passed"] is False


def test_run_groundtruth_nested_impl_file_gets_parent_dir(tmp_path):
    """impl_file may contain a "/" (e.g. "pkg/report.py"); the parent dir
    must be created in scratch before the copy (no extras involved)."""
    src = tmp_path / "src"
    _write_pkg_src(src)
    gt = harness.run_groundtruth(
        src, "pkg/report.py", GT_REPORT_ONLY, tmp_path / "scratch"
    )
    assert gt["ran"] is True
    assert gt["passed"] is True, gt.get("tail", "")


def test_run_groundtruth_missing_impl_file_reason_unchanged(tmp_path):
    """Regression: the pre-existing impl_file existence check and its
    reason string must survive the extras work (existing tasks rely on it)."""
    src = tmp_path / "src"
    src.mkdir()
    gt = harness.run_groundtruth(
        src,
        "pkg/report.py",
        GT_BOTH,
        tmp_path / "scratch",
        extra_impl_files=["pkg/__init__.py"],
    )
    assert gt["ran"] is False
    assert gt["passed"] is False
    assert gt["reason"] == f"no pkg/report.py at {src}"


def test_run_groundtruth_missing_extra_file_does_not_run_pytest(tmp_path, monkeypatch):
    """A missing extra file is a not-ran result naming the file - pytest
    must not even start."""
    src = tmp_path / "src"
    _write_pkg_src(src)
    (src / "pkg" / "api.py").unlink()

    def _boom(*args, **kwargs):
        raise AssertionError("pytest must not run when an extra file is missing")

    monkeypatch.setattr(harness.subprocess, "run", _boom)
    gt = harness.run_groundtruth(
        src,
        "pkg/report.py",
        GT_BOTH,
        tmp_path / "scratch",
        extra_impl_files=["pkg/__init__.py", "pkg/api.py"],
    )
    assert gt["ran"] is False
    assert gt["passed"] is False
    assert gt["reason"] == f"no pkg/api.py at {src}"
    # Extras are copied before impl_file, so the early return must leave
    # the impl file uncopied too.
    assert not (tmp_path / "scratch" / "pkg" / "report.py").exists()


# ---------- run_groundtruth: cargo / npm ecosystems ----------


@pytest.mark.parametrize(
    "ecosystem,impl_file",
    [
        ("cargo", "src/lib.rs"),
        ("npm", "src/lib.js"),
    ],
)
def test_run_groundtruth_non_pytest_rejects_extra_impl_files(
    tmp_path, monkeypatch, ecosystem, impl_file
):
    """A non-empty extra_impl_files on a cargo/npm task is a not-ran result
    with the pytest-only reason, and no subprocess may be started."""
    src = tmp_path / "src"
    (src / impl_file).parent.mkdir(parents=True)
    (src / impl_file).write_text("impl\n")

    def _boom(*args, **kwargs):
        raise AssertionError("no subprocess may start when extras are rejected")

    monkeypatch.setattr(harness.subprocess, "run", _boom)
    gt = harness.run_groundtruth(
        src,
        impl_file,
        "gt",
        tmp_path / "scratch",
        ecosystem=ecosystem,
        extra_impl_files=["src/other.rs"],
    )
    assert gt["ran"] is False
    assert gt["passed"] is False
    assert gt["reason"] == "extra_impl_files is only supported for pytest tasks"


@pytest.mark.parametrize(
    "ecosystem,impl_file,argv0",
    [
        ("cargo", "src/lib.rs", "cargo"),
        ("npm", "src/lib.js", "node"),
    ],
)
def test_run_groundtruth_non_pytest_empty_extras_still_runs(
    tmp_path, monkeypatch, ecosystem, impl_file, argv0
):
    """Empty extras must leave cargo/npm grading byte-identical: the real
    runner is still invoked (an empty list is not a rejection)."""
    src = tmp_path / "src"
    (src / impl_file).parent.mkdir(parents=True)
    (src / impl_file).write_text("impl\n")
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(harness.subprocess, "run", _fake_run)
    gt = harness.run_groundtruth(
        src,
        impl_file,
        "gt",
        tmp_path / "scratch",
        ecosystem=ecosystem,
        extra_impl_files=[],
    )
    assert gt["ran"] is True
    assert gt["passed"] is True
    assert calls and calls[0][0] == argv0


# ---------- _validate_extra_impl_files ----------


def test_validate_extra_impl_files_accepts_none_empty_and_valid():
    assert harness._validate_extra_impl_files("t", None) == []
    assert harness._validate_extra_impl_files("t", []) == []
    assert harness._validate_extra_impl_files("t", ["x.py"]) == ["x.py"]
    kept = harness._validate_extra_impl_files(
        "t", ["pkg/__init__.py", "pkg/api.py", "pkg/deep/util.py"]
    )
    assert kept == ["pkg/__init__.py", "pkg/api.py", "pkg/deep/util.py"]


@pytest.mark.parametrize(
    "entry",
    [
        "../x.py",
        "/abs.py",
        "a\\b.py",
        "pkg/../x.py",
        "",
        3,
    ],
)
def test_validate_extra_impl_files_rejects_bad_entry(entry):
    """Relative paths only: no leading "/", no backslash, no ".." component,
    no empty/non-str entries - and the error names the task and the entry."""
    with pytest.raises(ValueError) as ei:
        harness._validate_extra_impl_files("mytask", ["ok.py", entry])
    msg = str(ei.value)
    assert "mytask" in msg
    # The entry must be identifiable in the message, whether it was
    # interpolated as str(x) or repr(x) (a repr escapes the backslash).
    assert str(entry) in msg or repr(entry) in msg


def test_validate_extra_impl_files_rejects_duplicates():
    with pytest.raises(ValueError) as ei:
        harness._validate_extra_impl_files("mytask", ["a.py", "b.py", "a.py"])
    msg = str(ei.value)
    assert "mytask" in msg
    assert "a.py" in msg


@pytest.mark.parametrize("value", ["pkg/api.py", 3, {"pkg/api.py": 1}])
def test_validate_extra_impl_files_rejects_non_list(value):
    """The contract requires ValueError (not TypeError) for a non-list."""
    with pytest.raises(ValueError) as ei:
        harness._validate_extra_impl_files("mytask", value)
    assert "mytask" in str(ei.value)


def test_validate_extra_impl_files_value_error_is_suppressed_in_place():
    """The non-list raise stays a ValueError and carries its TRY004
    suppression on the raise line itself (ruff would otherwise flag it)."""
    raise_lines = [
        line
        for line in inspect.getsource(harness._validate_extra_impl_files).splitlines()
        if "raise ValueError" in line
    ]
    assert raise_lines, "validator must raise ValueError"
    assert any("TRY004" in line for line in raise_lines), (
        "the non-list raise needs its TRY004 suppression on the raise line"
    )


# ---------- load_task wiring ----------


def test_load_task_defaults_extra_impl_files_to_empty(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    _write_task(tasks, "plain", {"impl_file": "sol.py"})
    monkeypatch.setattr(harness, "TASKS_DIR", tasks)
    assert harness.load_task("plain")["extra_impl_files"] == []


def test_load_task_keeps_valid_extra_impl_files(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    _write_task(
        tasks,
        "multi",
        {
            "impl_file": "pkg/report.py",
            "extra_impl_files": ["pkg/__init__.py", "pkg/api.py"],
        },
    )
    monkeypatch.setattr(harness, "TASKS_DIR", tasks)
    spec = harness.load_task("multi")
    assert spec["extra_impl_files"] == ["pkg/__init__.py", "pkg/api.py"]


def test_load_task_normalizes_null_extra_impl_files(tmp_path, monkeypatch):
    """An explicit JSON null must come back as [] (the validator's None
    branch), not None - load_task routes the raw value through it."""
    tasks = tmp_path / "tasks"
    _write_task(tasks, "nullish", {"impl_file": "sol.py", "extra_impl_files": None})
    monkeypatch.setattr(harness, "TASKS_DIR", tasks)
    assert harness.load_task("nullish")["extra_impl_files"] == []


def test_load_task_rejects_invalid_extra_impl_files(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    _write_task(
        tasks, "bad", {"impl_file": "sol.py", "extra_impl_files": ["../evil.py"]}
    )
    monkeypatch.setattr(harness, "TASKS_DIR", tasks)
    with pytest.raises(ValueError) as ei:
        harness.load_task("bad")
    assert "bad" in str(ei.value)


def test_every_existing_task_loads_with_empty_extra_impl_files():
    """Every task that does not declare extras must still load and come back
    with extra_impl_files == [] (a guard on the defaulting, not a count pin -
    a later story may legitimately add a task that declares extras)."""
    names = sorted(p.parent.name for p in harness.TASKS_DIR.glob("*/spec.json"))
    assert names, "expected the checked-in benchmark tasks to be present"
    for name in names:
        spec = harness.load_task(name)
        if "extra_impl_files" in json.loads(
            (harness.TASKS_DIR / name / "spec.json").read_text()
        ):
            continue
        assert spec["extra_impl_files"] == [], name


# ---------- main() wiring ----------


def test_main_passes_extra_impl_files_to_run_groundtruth():
    assert "extra_impl_files=" in inspect.getsource(harness.main)
