"""OPSA-8: lint acceptance fixture sources at ingest time.

Covers the standalone helper (pipeline/fixture_lint.py) and the ingest_plan
wiring: a story whose .py acceptance entry fails lint REJECTS the ingest,
naming the entry path and the exact violations (fail closed). The sibling
test_acceptance_fixture_lint.py covers build_detect's advisory oracle lint —
a different failure class, left untouched here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline.fixture_lint import (
    RUFF_PINNED_VERSION,
    FixtureLintError,
    lint_acceptance_source,
)

CLEAN_SOURCE = '''\
"""A clean acceptance fixture."""


def test_ok() -> None:
    assert 1 + 1 == 2
'''

UNUSED_VAR_SOURCE = '''\
"""Fixture with an unused variable (RUF059-shaped)."""


def test_unused() -> None:
    unused_value = 42
    assert 1 + 1 == 2
'''


def _ruff_available() -> bool:
    found = shutil.which("ruff")
    if not found:
        return False
    proc = subprocess.run(
        [found, "--version"], capture_output=True, text=True, check=False
    )
    return proc.returncode == 0 and RUFF_PINNED_VERSION in proc.stdout


requires_ruff = pytest.mark.skipif(
    not _ruff_available(), reason="pinned ruff not on PATH"
)


@requires_ruff
def test_clean_source_returns_empty_list(tmp_path: Path) -> None:
    assert lint_acceptance_source(CLEAN_SOURCE, tmp_path) == []


@requires_ruff
def test_unused_variable_violation_is_reported(tmp_path: Path) -> None:
    violations = lint_acceptance_source(UNUSED_VAR_SOURCE, tmp_path)
    assert violations, "expected a non-empty violation list"
    joined = "\n".join(violations)
    assert "unused" in joined.lower() or "RUF" in joined


@requires_ruff
def test_multiple_violations_all_listed(tmp_path: Path) -> None:
    source = UNUSED_VAR_SOURCE + "\n\n\nx = 1\n"
    violations = lint_acceptance_source(source, tmp_path)
    assert len(violations) >= 2, violations


@requires_ruff
def test_temp_files_cleaned_up(tmp_path: Path) -> None:
    lint_acceptance_source(CLEAN_SOURCE, tmp_path)
    leftovers = list(tmp_path.parent.glob("opsa-fixture-lint-*"))
    assert leftovers == [], f"temp dirs left behind: {leftovers}"


def test_missing_ruff_binary_rejects_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ruff anywhere -> FixtureLintError, not a silent pass."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    with pytest.raises(FixtureLintError, match="not found"):
        lint_acceptance_source(CLEAN_SOURCE, tmp_path)


@requires_ruff
def test_non_string_source_rejects(tmp_path: Path) -> None:
    with pytest.raises(FixtureLintError, match="must be a string"):
        lint_acceptance_source(None, tmp_path)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ingest_plan wiring: a story whose .py acceptance entry fails lint REJECTS.
# ---------------------------------------------------------------------------


def _write_plan(plan_dir: Path, name: str, acceptance: list) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        "name": name,
        "repo_root": str(plan_dir),
        "epics": [
            {
                "summary": "Epic",
                "stories": [
                    {
                        "key": "S1",
                        "summary": "Story",
                        "acceptance": acceptance,
                    }
                ],
            }
        ],
    }
    (plan_dir / f"{name}.json").write_text(json.dumps(plan))


def _py_entry(source: str, path: str = "tests/unit/test_fixture.py") -> dict:
    return {"path": path, "source": source}


def _patch_plan_dir(monkeypatch: pytest.MonkeyPatch, plan_dir: Path) -> None:
    """Patch the PLAN_DIR bindings the conftest plan_dir fixture patches."""
    import pipeline.server as p
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers

    monkeypatch.setattr(p, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(ppers, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(pcon, "PLAN_DIR", plan_dir)


@requires_ruff
def test_ingest_rejects_failing_fixture_source(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pipeline.ingest as ingest_mod

    _write_plan(plan_dir, "lint-bad", [_py_entry(UNUSED_VAR_SOURCE)])
    _patch_plan_dir(monkeypatch, plan_dir)
    result = ingest_mod._ingest_plan_impl("lint-bad")
    assert result["ok"] is False
    assert "unused" in result["error"].lower() or "RUF" in result["error"]


@requires_ruff
def test_ingest_allows_clean_fixture_source(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pipeline.ingest as ingest_mod

    _write_plan(plan_dir, "lint-clean", [_py_entry(CLEAN_SOURCE)])
    _patch_plan_dir(monkeypatch, plan_dir)
    result = ingest_mod._ingest_plan_impl("lint-clean")
    assert result["ok"] is True, result


@requires_ruff
def test_ingest_skips_non_py_acceptance_paths(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pipeline.ingest as ingest_mod

    _write_plan(plan_dir, "lint-nonpy", [_py_entry(CLEAN_SOURCE, "docs/spec.md")])
    _patch_plan_dir(monkeypatch, plan_dir)
    result = ingest_mod._ingest_plan_impl("lint-nonpy")
    assert result["ok"] is True, result


def test_ingest_rejects_when_ruff_unavailable(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: ruff missing -> ingest rejects naming the cause."""
    import pipeline.ingest as ingest_mod

    _write_plan(plan_dir, "lint-noruff", [_py_entry(CLEAN_SOURCE)])
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    _patch_plan_dir(monkeypatch, plan_dir)
    result = ingest_mod._ingest_plan_impl("lint-noruff")
    assert result["ok"] is False
    assert "ruff" in result["error"].lower()