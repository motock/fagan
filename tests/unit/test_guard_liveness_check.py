"""Tests for ``pipeline.guard_liveness.check_guard_liveness`` (liveness story).

Contract under test:

- ``check_guard_liveness(dataset, repo_root, collected_test_files=None)``
  consumes the failure-mode dataset SHAPE (a list of dicts carrying
  ``mode``/``status``/``guard`` keys) plus a repo root, and reports for every
  entry whether each cited guard test file EXISTS under ``<repo_root>/tests/``
  and -- when the caller supplies pytest-collected test files -- whether it is
  COLLECTED.  Pure: no subprocess, no network I/O.

STUB-THE-CONFIG-SOURCE RULE: these tests NEVER read or assert against the
real ``docs/failure_modes.json`` or the real repository tree.  Every dataset
below is an inline synthetic list of dicts, and every repo tree is a
synthetic ``tmp_path`` layout whose ``tests/`` files are plain text (the
checker must never import or execute them).  The live 25/51 confirmed-guard
state appears nowhere in these assertions.

Pinned decisions (where the brief left the form open):

- ``missing`` lists the cited candidate names verbatim (the same strings as
  ``guard_files``), one per candidate with no matching file under tests/.
- ``uncollected`` lists the tests/-relative paths of the EXISTING guard files
  that are absent from ``collected_test_files`` -- so a basename present both
  at ``tests/test_x.py`` and ``tests/benchmark/test_x.py`` can have one copy
  collected and the other flagged.
- Collected entries may be repo-relative (``tests/test_x.py``) or bare names
  (``test_x.py``); both count as collecting the matching instance.
- ``uncollected`` entries are tests/-relative instance paths (e.g.
  ``benchmark/test_twin_guard.py``); a bare-name candidate that exists at
  several tests/ locations flags every existing instance not collected.
- A missing ``guard`` key yields ``guard_files == []`` (no candidates to
  parse); a missing ``status`` key yields ``expected_live == False``; a
  missing ``mode`` key yields ``mode == ""``.
- Non-dict dataset entries (str, None, ...) are skipped entirely: no entry
  record and NOT counted in ``summary["total"]``.
- ``with_guard``/``no_guard_expected`` partition ``total`` by whether the
  entry parsed any guard-file candidate (``guard_files`` empty or not).
"""

import inspect
import os
import socket
import subprocess
from pathlib import Path

import pytest

from pipeline.guard_liveness import (
    check_guard_liveness,
    is_no_guard_note,
    parse_guard_paths,
)

ENTRY_KEYS = {
    "mode",
    "status",
    "guard_note",
    "expected_live",
    "guard_files",
    "missing",
    "uncollected",
}
SUMMARY_KEYS = {
    "total",
    "with_guard",
    "no_guard_expected",
    "missing_files",
    "uncollected_files",
}
EMPTY_SUMMARY = {
    "total": 0,
    "with_guard": 0,
    "no_guard_expected": 0,
    "missing_files": 0,
    "uncollected_files": 0,
}

# Plain text on purpose: if the checker ever imports/executes the synthetic
# guard files instead of only stat-ing them, these tests blow up.
_PLACEHOLDER = "synthetic guard file - plain text, never real python\n"


def _entry(mode, status, guard):
    """Return one synthetic dataset entry with the real dataset's shape."""
    return {
        "mode": mode,
        "date": "2026-01-01",
        "class": "harness-bug",
        "status": status,
        "fix_ref": "PR #0",
        "guard": guard,
    }


def _make_repo(tmp_path, files):
    """Create a synthetic repo tree; ``files`` maps repo-relative path -> text."""
    root = tmp_path / "repo"
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_function_signature_matches_brief():
    sig = inspect.signature(check_guard_liveness)
    assert list(sig.parameters) == ["dataset", "repo_root", "collected_test_files"]
    assert sig.parameters["dataset"].default is inspect.Parameter.empty
    assert sig.parameters["repo_root"].default is inspect.Parameter.empty
    assert sig.parameters["collected_test_files"].default is None


def test_report_shape_exact_keys(tmp_path):
    root = _make_repo(tmp_path, {"tests/test_alpha_guard.py": _PLACEHOLDER})
    dataset = [_entry("1", "FIXED", "`test_alpha_guard.py`")]
    report = check_guard_liveness(dataset, root)
    assert set(report) == {"entries", "summary"}
    assert isinstance(report["entries"], list)
    assert len(report["entries"]) == 1
    assert set(report["entries"][0]) == ENTRY_KEYS
    assert set(report["summary"]) == SUMMARY_KEYS
    entry = report["entries"][0]
    assert entry["expected_live"] is True
    assert entry["uncollected"] == []  # collection list not supplied
    assert isinstance(entry["guard_files"], list)
    assert isinstance(entry["missing"], list)
    assert isinstance(entry["uncollected"], list)
    for key in SUMMARY_KEYS:
        assert isinstance(report["summary"][key], int)


def test_empty_dataset_reports_all_zero_summary(tmp_path):
    root = _make_repo(tmp_path, {"tests/test_alpha_guard.py": _PLACEHOLDER})
    report = check_guard_liveness([], root, ["tests/test_alpha_guard.py"])
    assert report == {"entries": [], "summary": dict(EMPTY_SUMMARY)}


def test_expected_live_is_case_sensitive_fixed_substring(tmp_path):
    root = _make_repo(tmp_path, {"tests/test_unrelated_guard.py": _PLACEHOLDER})
    statuses = [
        ("FIXED", True),
        ("FIXED (with 28)", True),
        ("FIXED (verified live end-to-end)", True),
        ("NOT fixed (workaround only)", False),
        ("fixed (lowercase only)", False),
        ("n/a", False),
        ("PARTIAL", False),
        ("recurring", False),
        ("MITIGATED (root cause not pinned)", False),
        ("REFRAMED - no defect found (observability gap fixed)", False),
    ]
    dataset = [
        _entry(str(i), status, "none identified")
        for i, (status, _) in enumerate(statuses, start=1)
    ]
    report = check_guard_liveness(dataset, root)
    for entry, (_, expected) in zip(report["entries"], statuses):
        assert entry["expected_live"] is expected, entry["status"]
    assert report["summary"] == {
        "total": 10,
        "with_guard": 0,
        "no_guard_expected": 10,
        "missing_files": 0,
        "uncollected_files": 0,
    }


def test_expected_live_depends_only_on_status_not_guard(tmp_path):
    root = _make_repo(tmp_path, {"tests/test_delta_guard.py": _PLACEHOLDER})
    dataset = [
        _entry("1", "FIXED", "none identified"),
        _entry("2", "NOT fixed (workaround only)", "`test_delta_guard.py`"),
    ]
    report = check_guard_liveness(dataset, root, ["tests/test_delta_guard.py"])
    first, second = report["entries"]
    assert first["expected_live"] is True
    assert first["guard_files"] == []
    assert first["missing"] == []
    assert first["uncollected"] == []
    assert second["expected_live"] is False
    assert second["guard_files"] == ["test_delta_guard.py"]
    assert second["missing"] == []
    assert second["uncollected"] == []
    assert report["summary"] == {
        "total": 2,
        "with_guard": 1,
        "no_guard_expected": 1,
        "missing_files": 0,
        "uncollected_files": 0,
    }


def test_full_report_on_mixed_synthetic_dataset(tmp_path):
    root = _make_repo(
        tmp_path,
        {
            "tests/test_alpha_guard.py": _PLACEHOLDER,
            "tests/test_delta_guard.py": _PLACEHOLDER,
            "tests/nested/test_epsilon_guard.py": _PLACEHOLDER,
        },
    )
    dataset = [
        _entry("1", "FIXED", "`test_alpha_guard.py` (`ALPHA_CONST`)"),
        _entry("2", "FIXED (with 28)", "`test_beta_guard.py` + `test_gamma_guard.py`"),
        _entry("3", "FIXED (verified live end-to-end)", "none identified"),
        _entry("4", "NOT fixed (workaround only)", "`test_delta_guard.py`"),
        _entry("5", "PARTIAL", ""),
        _entry("6", "recurring", "`test_epsilon_guard.py`"),
    ]
    collected = ["tests/test_alpha_guard.py", "tests/nested/test_epsilon_guard.py"]
    report = check_guard_liveness(dataset, root, collected)
    assert report["entries"][0] == {
        "mode": "1",
        "status": "FIXED",
        "guard_note": "`test_alpha_guard.py` (`ALPHA_CONST`)",
        "expected_live": True,
        "guard_files": ["test_alpha_guard.py"],
        "missing": [],
        "uncollected": [],
    }
    assert report["entries"][1] == {
        "mode": "2",
        "status": "FIXED (with 28)",
        "guard_note": "`test_beta_guard.py` + `test_gamma_guard.py`",
        "expected_live": True,
        "guard_files": ["test_beta_guard.py", "test_gamma_guard.py"],
        "missing": ["test_beta_guard.py", "test_gamma_guard.py"],
        "uncollected": [],
    }
    assert report["entries"][2] == {
        "mode": "3",
        "status": "FIXED (verified live end-to-end)",
        "guard_note": "none identified",
        "expected_live": True,
        "guard_files": [],
        "missing": [],
        "uncollected": [],
    }
    assert report["entries"][3] == {
        "mode": "4",
        "status": "NOT fixed (workaround only)",
        "guard_note": "`test_delta_guard.py`",
        "expected_live": False,
        "guard_files": ["test_delta_guard.py"],
        "missing": [],
        "uncollected": ["test_delta_guard.py"],
    }
    assert report["entries"][4] == {
        "mode": "5",
        "status": "PARTIAL",
        "guard_note": "",
        "expected_live": False,
        "guard_files": [],
        "missing": [],
        "uncollected": [],
    }
    assert report["entries"][5] == {
        "mode": "6",
        "status": "recurring",
        "guard_note": "`test_epsilon_guard.py`",
        "expected_live": False,
        "guard_files": ["test_epsilon_guard.py"],
        "missing": [],
        "uncollected": [],
    }
    assert [e["mode"] for e in report["entries"]] == ["1", "2", "3", "4", "5", "6"]
    assert report["summary"] == {
        "total": 6,
        "with_guard": 4,
        "no_guard_expected": 2,
        "missing_files": 1,
        "uncollected_files": 1,
    }


def test_existence_search_is_recursive_under_tests(tmp_path):
    root = _make_repo(
        tmp_path,
        {
            "tests/unit/test_deep_guard.py": _PLACEHOLDER,
            "tests/test_shallow_guard.py": _PLACEHOLDER,
        },
    )
    dataset = [
        _entry("1", "FIXED", "`test_deep_guard.py`"),
        _entry("2", "FIXED", "`tests/unit/test_deep_guard.py`"),
        _entry("3", "FIXED", "`tests/unit/test_absent_guard.py`"),
    ]
    report = check_guard_liveness(dataset, root)
    assert report["entries"][0]["guard_files"] == ["test_deep_guard.py"]
    assert report["entries"][0]["missing"] == []
    assert report["entries"][1]["guard_files"] == ["tests/unit/test_deep_guard.py"]
    assert report["entries"][1]["missing"] == []
    assert report["entries"][2]["guard_files"] == ["tests/unit/test_absent_guard.py"]
    assert report["entries"][2]["missing"] == ["tests/unit/test_absent_guard.py"]
    assert report["summary"]["missing_files"] == 1


def test_candidate_outside_tests_never_counts_as_found(tmp_path):
    root = _make_repo(
        tmp_path,
        {
            "test_outside_guard.py": _PLACEHOLDER,
            "pipeline/test_outside_guard.py": _PLACEHOLDER,
            "tests/test_other_guard.py": _PLACEHOLDER,
        },
    )
    dataset = [_entry("1", "FIXED", "`test_outside_guard.py`")]
    report = check_guard_liveness(dataset, root)
    assert report["entries"][0]["missing"] == ["test_outside_guard.py"]
    assert report["entries"][0]["uncollected"] == []
    assert report["summary"]["missing_files"] == 1


def test_repo_without_tests_dir_treats_every_candidate_as_missing(tmp_path):
    root = tmp_path / "bare_repo"
    root.mkdir()
    dataset = [_entry("1", "FIXED", "`test_lonely_guard.py`")]
    report = check_guard_liveness(dataset, root)
    assert report["entries"][0]["guard_files"] == ["test_lonely_guard.py"]
    assert report["entries"][0]["missing"] == ["test_lonely_guard.py"]
    assert report["summary"]["missing_files"] == 1


def test_uncollected_uses_supplied_collection_list(tmp_path):
    root = _make_repo(tmp_path, {"tests/test_alpha_guard.py": _PLACEHOLDER})
    dataset = [_entry("1", "FIXED", "`test_alpha_guard.py`")]
    # repo-relative collected path -> the guard file counts as collected
    report = check_guard_liveness(dataset, root, ["tests/test_alpha_guard.py"])
    assert report["entries"][0]["uncollected"] == []
    assert report["summary"]["uncollected_files"] == 0
    # bare collected name -> also counts as collected
    report = check_guard_liveness(dataset, root, ["test_alpha_guard.py"])
    assert report["entries"][0]["uncollected"] == []
    # collected list without the guard file -> flagged
    report = check_guard_liveness(dataset, root, ["tests/test_other_guard.py"])
    assert report["entries"][0]["uncollected"] == ["test_alpha_guard.py"]
    assert report["summary"]["uncollected_files"] == 1
    # empty (but supplied) collection list -> flagged
    report = check_guard_liveness(dataset, root, [])
    assert report["entries"][0]["uncollected"] == ["test_alpha_guard.py"]


def test_collected_none_leaves_uncollected_empty_everywhere(tmp_path):
    root = _make_repo(
        tmp_path,
        {
            "tests/test_alpha_guard.py": _PLACEHOLDER,
            "tests/test_beta_guard.py": _PLACEHOLDER,
        },
    )
    dataset = [
        _entry("1", "FIXED", "`test_alpha_guard.py`"),
        _entry("2", "FIXED (with 28)", "`test_beta_guard.py` + `test_absent_guard.py`"),
        _entry("3", "NOT fixed", "none identified"),
    ]
    report = check_guard_liveness(dataset, root, None)
    assert all(e["uncollected"] == [] for e in report["entries"])
    assert report["summary"]["uncollected_files"] == 0
    assert report["entries"][1]["missing"] == ["test_absent_guard.py"]
    # omitting the argument behaves exactly like passing None
    assert check_guard_liveness(dataset, root) == report


def test_duplicate_basename_collected_only_in_benchmark(tmp_path):
    root = _make_repo(
        tmp_path,
        {
            "tests/test_twin_guard.py": _PLACEHOLDER,
            "tests/benchmark/test_twin_guard.py": _PLACEHOLDER,
        },
    )
    dataset = [_entry("1", "FIXED", "`test_twin_guard.py`")]
    report = check_guard_liveness(dataset, root, ["tests/benchmark/test_twin_guard.py"])
    entry = report["entries"][0]
    assert entry["guard_files"] == ["test_twin_guard.py"]
    assert entry["missing"] == []  # the name exists under tests/
    assert entry["uncollected"] == ["test_twin_guard.py"]  # the tests/ copy is dark
    assert report["summary"]["missing_files"] == 0
    assert report["summary"]["uncollected_files"] == 1


def test_uncollected_entries_are_tests_relative_instance_paths(tmp_path):
    root = _make_repo(tmp_path, {"tests/benchmark/test_twin_guard.py": _PLACEHOLDER})
    dataset = [_entry("1", "FIXED", "`tests/benchmark/test_twin_guard.py`")]
    report = check_guard_liveness(dataset, root, [])
    assert report["entries"][0]["missing"] == []
    assert report["entries"][0]["uncollected"] == ["benchmark/test_twin_guard.py"]


def test_summary_counts_entries_not_files(tmp_path):
    root = _make_repo(
        tmp_path,
        {
            "tests/test_u1_guard.py": _PLACEHOLDER,
            "tests/test_twin_guard.py": _PLACEHOLDER,
            "tests/benchmark/test_twin_guard.py": _PLACEHOLDER,
        },
    )
    dataset = [
        _entry("1", "FIXED", "`test_m1_guard.py` + `test_m2_guard.py`"),
        _entry("2", "FIXED", "`test_u1_guard.py`"),
        _entry("3", "FIXED", "`test_twin_guard.py`"),
    ]
    report = check_guard_liveness(dataset, root, [])
    assert report["summary"] == {
        "total": 3,
        "with_guard": 3,
        "no_guard_expected": 0,
        "missing_files": 1,  # entry 1, despite two missing files
        "uncollected_files": 2,  # entries 2 and 3
    }
    assert report["entries"][0]["missing"] == ["test_m1_guard.py", "test_m2_guard.py"]
    # walk order must not leak into the report: compare as a set
    assert sorted(report["entries"][2]["uncollected"]) == [
        "benchmark/test_twin_guard.py",
        "test_twin_guard.py",
    ]


def test_only_no_guard_entries(tmp_path):
    root = _make_repo(tmp_path, {"tests/test_alpha_guard.py": _PLACEHOLDER})
    dataset = [
        _entry("1", "FIXED", "none identified"),
        _entry("2", "NOT fixed (workaround only)", "None identified"),
    ]
    report = check_guard_liveness(dataset, root, ["tests/test_alpha_guard.py"])
    for entry in report["entries"]:
        assert entry["guard_files"] == []
        assert entry["missing"] == []  # a no-guard entry never appears in missing
        assert entry["uncollected"] == []
    assert report["summary"] == {
        "total": 2,
        "with_guard": 0,
        "no_guard_expected": 2,
        "missing_files": 0,
        "uncollected_files": 0,
    }


def test_entry_missing_guard_status_mode_keys_does_not_crash(tmp_path):
    root = _make_repo(tmp_path, {"tests/test_ok_guard.py": _PLACEHOLDER})
    dataset = [
        {},  # missing mode, status AND guard
        _entry("7", "FIXED", "`test_ok_guard.py`"),
    ]
    report = check_guard_liveness(dataset, root, ["tests/test_ok_guard.py"])
    assert report["entries"][0] == {
        "mode": "",
        "status": "",
        "guard_note": "",
        "expected_live": False,
        "guard_files": [],
        "missing": [],
        "uncollected": [],
    }
    assert report["entries"][1]["mode"] == "7"
    assert report["summary"]["total"] == 2  # the malformed entry is still counted
    assert report["summary"]["with_guard"] == 1
    assert report["summary"]["no_guard_expected"] == 1
    assert report["summary"]["missing_files"] == 0


def test_entries_missing_individual_keys(tmp_path):
    root = _make_repo(tmp_path, {"tests/test_statusless_guard.py": _PLACEHOLDER})
    dataset = [
        {"mode": "8", "status": "FIXED", "fix_ref": "PR #1"},  # no guard key
        {"mode": "9", "guard": "`test_statusless_guard.py`"},  # no status key
        {"status": "FIXED", "guard": "none identified"},  # no mode key
    ]
    report = check_guard_liveness(dataset, root)
    e8, e9, e10 = report["entries"]
    assert e8 == {
        "mode": "8",
        "status": "FIXED",
        "guard_note": "",
        "expected_live": True,
        "guard_files": [],
        "missing": [],
        "uncollected": [],
    }
    assert e9["expected_live"] is False  # absent status never counts as FIXED
    assert e9["guard_files"] == ["test_statusless_guard.py"]
    assert e9["missing"] == []  # the cited file DOES exist in this repo
    assert e10["mode"] == ""
    assert e10["expected_live"] is True
    assert report["summary"] == {
        "total": 3,
        "with_guard": 1,
        "no_guard_expected": 2,
        "missing_files": 0,
        "uncollected_files": 0,
    }


def test_non_dict_entries_are_skipped(tmp_path):
    root = _make_repo(tmp_path, {"tests/test_alpha_guard.py": _PLACEHOLDER})
    dataset = [
        "not-a-dict",
        None,
        _entry("1", "FIXED", "`test_alpha_guard.py`"),
    ]
    report = check_guard_liveness(dataset, root, ["tests/test_alpha_guard.py"])
    assert [e["mode"] for e in report["entries"]] == ["1"]
    assert report["summary"]["total"] == 1


def test_type_errors_on_malformed_arguments(tmp_path):
    root = _make_repo(tmp_path, {"tests/test_alpha_guard.py": _PLACEHOLDER})
    with pytest.raises(TypeError, match="dataset"):
        check_guard_liveness(None, root)
    with pytest.raises(TypeError, match="dataset"):
        check_guard_liveness("tests/test_alpha_guard.py", root)
    with pytest.raises(TypeError, match="collected_test_files"):
        check_guard_liveness(
            [_entry("1", "FIXED", "`test_alpha_guard.py`")],
            root,
            "tests/test_alpha_guard.py",
        )


def test_dataset_fields_are_copied_verbatim(tmp_path):
    root = _make_repo(tmp_path, {"tests/test_alpha_guard.py": _PLACEHOLDER})
    dataset = [
        _entry(
            7,
            "FIXED (with 28)",
            "`test_alpha_guard.py` (added later, 2026-08-03, as the durable guard)",
        ),
    ]
    report = check_guard_liveness(dataset, root)
    entry = report["entries"][0]
    assert entry["mode"] == 7  # copied verbatim, not str()-coerced
    assert entry["status"] == "FIXED (with 28)"
    assert entry["guard_note"] == (
        "`test_alpha_guard.py` (added later, 2026-08-03, as the durable guard)"
    )
    assert entry["guard_files"] == ["test_alpha_guard.py"]
    assert entry["expected_live"] is True


def test_duplicate_candidates_are_deduplicated(tmp_path):
    root = _make_repo(tmp_path, {})  # no tests/ directory at all
    dataset = [_entry("1", "FIXED", "`test_dup_guard.py` and again `test_dup_guard.py`")]
    report = check_guard_liveness(dataset, root)
    assert report["entries"][0]["guard_files"] == ["test_dup_guard.py"]
    assert report["entries"][0]["missing"] == ["test_dup_guard.py"]
    assert report["summary"]["missing_files"] == 1


def test_check_guard_liveness_does_no_subprocess_and_no_network(tmp_path, monkeypatch):
    root = _make_repo(tmp_path, {"tests/test_alpha_guard.py": _PLACEHOLDER})
    dataset = [_entry("1", "FIXED", "`test_alpha_guard.py`")]

    def _forbidden(name):
        def _raise(*args, **kwargs):
            raise AssertionError(f"check_guard_liveness must not use {name}")

        return _raise

    monkeypatch.setattr(subprocess, "Popen", _forbidden("subprocess.Popen"))
    monkeypatch.setattr(subprocess, "run", _forbidden("subprocess.run"))
    monkeypatch.setattr(subprocess, "check_call", _forbidden("subprocess.check_call"))
    monkeypatch.setattr(subprocess, "check_output", _forbidden("subprocess.check_output"))
    monkeypatch.setattr(os, "system", _forbidden("os.system"))
    monkeypatch.setattr(os, "popen", _forbidden("os.popen"))
    monkeypatch.setattr(socket, "socket", _forbidden("socket.socket"))
    monkeypatch.setattr(socket, "create_connection", _forbidden("socket.create_connection"))

    report = check_guard_liveness(dataset, root, ["tests/test_alpha_guard.py"])
    assert report["summary"]["total"] == 1

    # ...and the module must not even import the I/O machinery
    source_path = inspect.getsourcefile(check_guard_liveness)
    source = Path(source_path).read_text(encoding="utf-8")
    for banned in (
        "import subprocess",
        "from subprocess",
        "import socket",
        "from socket",
        "import urllib",
        "from urllib",
        "import requests",
    ):
        assert banned not in source


def test_prior_story_parsing_surface_still_intact():
    assert parse_guard_paths("`test_a.py` + `test_b.py`") == ["test_a.py", "test_b.py"]
    assert parse_guard_paths("none identified") == []
    assert is_no_guard_note("none identified") is True
    assert is_no_guard_note("`test_a.py` (none found)") is False