"""OPSA-8: the ingest-time acceptance-fixture lint gate.

This module pins the contract the reviewer required on this branch
(REQUEST_CHANGES verdict):

1. There is exactly ONE lint gate. ``pipeline/build_detect._lint_acceptance_fixtures``
   already implements it (its docstring calls itself "groundwork only" and
   leaves "wiring it into the ingest-time per-story loop (and re-exporting it
   alongside its siblings)" to this follow-up story). ``pipeline/fixture_lint.py``
   - a second, parallel implementation with different rule selection, different
   failure semantics and a different output shape - must be deleted, and
   ``pipeline.server`` must re-export the existing helper.
2. The gate runs ruff with its DEFAULT rule set, so F401 (unused import) - the
   exact rule behind the PR #235 incident ("the plan author's own fixture
   carried an unused import (ruff F401)") - is enforced. A fixture whose only
   violation is an unused import must reject the ingest.
3. Tool absence is FAIL-OPEN. The helper's docstring is explicit: callers
   "must only ever gate on 'finding', never on 'skipped', so a broken or absent
   lint tool can never block a known-good plan". An absent ruff must therefore
   not reject a clean plan.
4. ruff is resolved from the PIPELINE's own environment, never from the target
   repo's ``.venv``: ``repo_root`` is the TARGET project (README.md:92,
   REFERENCE.md:593), so a target repo pinning a different ruff must not fail
   every ingest of a plan for that repo.
5. The lint runs in the UPFRONT validation loop, before any Plane side effect
   (``create_epic``/``create_story``), so a rejected plan leaves no orphaned
   epics/stories behind and a re-run is clean.

The sibling ``test_acceptance_fixture_lint.py`` covers the helper itself; this
module covers the ingest wiring.

FLAGGED FOR THE TEST OWNER (not edited here): once the helper is wired,
``tests/unit/test_pipeline_mcp_server_decisions_and_dispatch.py::test_ingest_plan_warns_on_isolation_only_acceptance_fixture``
breaks. It embeds the acceptance source ``def test_x():\\n    assert
_no_tool_nudge(0)\\n`` (path ``tests/test_nudge.py``) and asserts
``result["ok"] is True``; ruff's DEFAULT rule set includes F821 (undefined
name), so the wired gate reports a finding and ingest rejects. That fixture
source needs to become lint-clean (e.g. define ``_no_tool_nudge`` in the
source) while staying isolation-only - i.e. free of the integration markers
``_isolation_only_acceptance_warning`` looks for.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline import build_detect as bd
from pipeline import server as p

CLEAN_SOURCE = '''\
"""A clean acceptance fixture."""


def test_ok() -> None:
    assert 1 + 1 == 2
'''

# The PR #235 failure class: the plan author's own fixture carried an unused
# import. ruff's DEFAULT rule set flags this as F401.
F401_SOURCE = '''\
"""Fixture with an unused import (PR #235)."""

import pytest


def test_acceptance() -> None:
    assert 1 + 1 == 2
'''

F841_SOURCE = '''\
"""Fixture with an assigned-but-unused local (F841)."""


def test_unused() -> None:
    unused_value = 42
    assert 1 + 1 == 2
'''

SYNTAX_ERROR_SOURCE = '''\
"""Fixture with a syntax error (E9)."""


def test_broken(:
    assert True
'''

# --------------------------------------------------------------------------- #
# Stub ruff binaries.  They emulate the ONE behaviour under test - which rules
# the caller asked for - so the rule-selection contract can be pinned without
# depending on a real ruff install.  ruff's defaults include the F rules
# (F401); a --select list that omits F401 does not report it.
# --------------------------------------------------------------------------- #
FAKE_RUFF_HONOURS_SELECT = """#!/bin/sh
select=""
prev=""
for a in "$@"; do
  if [ "$a" = "--version" ]; then echo "ruff 0.16.5"; exit 0; fi
  if [ "$prev" = "--select" ]; then select="$a"; fi
  prev="$a"
done
for last; do :; done
if grep -rq "import pytest" "$last" 2>/dev/null; then
  case "$select" in
    ""|*ALL*|*F401*) echo "tests/unit/test_fixture.py:3:8: F401 'pytest' imported but unused"; exit 1 ;;
  esac
fi
exit 0
"""

FAKE_RUFF_REPORTS_F841 = """#!/bin/sh
for a in "$@"; do
  if [ "$a" = "--version" ]; then echo "ruff 0.16.5"; exit 0; fi
done
for last; do :; done
if grep -rq "unused_value" "$last" 2>/dev/null; then
  echo "tests/unit/test_fixture.py:5:5: F841 local variable 'unused_value' is assigned to but never used"
  exit 1
fi
exit 0
"""

FAKE_RUFF_CLEAN = """#!/bin/sh
for a in "$@"; do
  if [ "$a" = "--version" ]; then echo "ruff 0.16.5"; exit 0; fi
done
exit 0
"""

# A ruff that is NOT the pipeline's pinned 0.16.5 - used to stand in for a
# target repo's own .venv/bin/ruff.
FAKE_RUFF_OLD_VERSION = """#!/bin/sh
for a in "$@"; do
  if [ "$a" = "--version" ]; then echo "ruff 0.4.0"; exit 0; fi
done
exit 0
"""


def _real_ruff_available() -> bool:
    found = shutil.which("ruff")
    if not found:
        return False
    proc = subprocess.run(
        [found, "--version"], capture_output=True, text=True, check=False
    )
    return proc.returncode == 0


requires_real_ruff = pytest.mark.skipif(
    not _real_ruff_available(), reason="ruff not installed"
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _write_script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _use_ruff(monkeypatch: pytest.MonkeyPatch, ruff_path: Path) -> None:
    """Make ``shutil.which("ruff")`` resolve to the PIPELINE's ruff."""
    real_which = shutil.which

    def fake_which(name, *args, **kwargs):
        if name == "ruff":
            return str(ruff_path)
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", fake_which)


def _hide_ruff(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ruff anywhere in the pipeline's environment."""
    real_which = shutil.which

    def fake_which(name, *args, **kwargs):
        if name == "ruff":
            return None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", fake_which)


def _py_entry(source: str, path: str = "tests/unit/test_fixture.py") -> dict:
    return {"path": path, "source": source}


def _story(key: str, acceptance: list[dict]) -> dict:
    return {"key": key, "summary": f"Story {key}", "acceptance": acceptance}


def _write_plan(
    plan_dir: Path,
    name: str,
    stories: list[dict],
    repo_root: Path | None = None,
) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        "name": name,
        "repo_root": str(repo_root or plan_dir),
        "epics": [{"summary": "Epic", "stories": stories}],
    }
    (plan_dir / f"{name}.json").write_text(json.dumps(plan))


def _patch_plan_dir(monkeypatch: pytest.MonkeyPatch, plan_dir: Path) -> None:
    """Patch the PLAN_DIR bindings the conftest plan_dir fixture patches."""
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers

    monkeypatch.setattr(p, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(ppers, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(pcon, "PLAN_DIR", plan_dir)


def _ingest(plan_name: str) -> dict:
    from pipeline.ingest import _ingest_plan_impl

    return _ingest_plan_impl(plan_name)


class _RecordingProvider:
    """Ticket provider that records every Plane side effect."""

    enabled = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def create_epic(self, summary: str):
        self.calls.append(("create_epic", summary))

    def create_story(self, summary, description, epic_id, agent):
        self.calls.append(("create_story", summary))


# --------------------------------------------------------------------------- #
# 1. One gate, not two: the parallel implementation is gone, the existing
#    helper is wired into ingest and re-exported by the server.
# --------------------------------------------------------------------------- #
def test_parallel_fixture_lint_module_is_deleted() -> None:
    """pipeline/fixture_lint.py reimplemented build_detect's gate; delete it."""
    import pipeline

    module_path = Path(pipeline.__file__).resolve().parent / "fixture_lint.py"
    assert not module_path.exists(), (
        f"{module_path} still exists: it is a second, parallel implementation "
        "of build_detect._lint_acceptance_fixtures (different rule selection, "
        "different failure semantics, different output shape)"
    )
    assert importlib.util.find_spec("pipeline.fixture_lint") is None


def test_server_reexports_the_existing_helper() -> None:
    """The helper must be re-exported alongside its build_detect siblings."""
    assert (
        getattr(p, "_lint_acceptance_fixtures", None) is bd._lint_acceptance_fixtures
    ), "pipeline.server does not re-export build_detect._lint_acceptance_fixtures"


def test_ingest_calls_the_existing_helper() -> None:
    """ingest must call the existing helper, not a parallel implementation."""
    from pipeline import ingest as ingest_mod

    assert (
        getattr(ingest_mod, "_lint_acceptance_fixtures", None)
        is bd._lint_acceptance_fixtures
    ), "pipeline.ingest does not use build_detect._lint_acceptance_fixtures"


# --------------------------------------------------------------------------- #
# 2. F401 (unused import) - the PR #235 failure class - must be enforced.
# --------------------------------------------------------------------------- #
def test_gate_asks_ruff_for_f401_unused_import(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gate must run ruff's DEFAULT rules, so F401 fires."""
    ruff = _write_script(tmp_path / "fakebin" / "ruff", FAKE_RUFF_HONOURS_SELECT)
    _use_ruff(monkeypatch, ruff)
    _write_plan(plan_dir, "f401", [_story("S1", [_py_entry(F401_SOURCE)])])
    _patch_plan_dir(monkeypatch, plan_dir)

    result = _ingest("f401")

    assert result["ok"] is False, (
        "a fixture whose only violation is an unused import (F401) was "
        f"accepted: the gate is not using ruff's default rule set: {result}"
    )
    assert "F401" in result["error"], result


@requires_real_ruff
def test_ingest_rejects_fixture_with_unused_import_end_to_end(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end with the real ruff: F401 rejects the ingest."""
    _write_plan(plan_dir, "f401-real", [_story("S1", [_py_entry(F401_SOURCE)])])
    _patch_plan_dir(monkeypatch, plan_dir)

    result = _ingest("f401-real")

    assert result["ok"] is False, f"unused import (F401) fixture was accepted: {result}"
    assert "F401" in result["error"], result


# --------------------------------------------------------------------------- #
# 3. Fail-open on tool absence (documented contract).
# --------------------------------------------------------------------------- #
def test_ingest_succeeds_when_ruff_is_absent(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent lint tool must never block a known-good plan."""
    _hide_ruff(monkeypatch)
    _write_plan(plan_dir, "noruff", [_story("S1", [_py_entry(CLEAN_SOURCE)])])
    _patch_plan_dir(monkeypatch, plan_dir)

    result = _ingest("noruff")

    assert result["ok"] is True, (
        "an absent ruff rejected a clean plan; callers must only ever gate on "
        f"'finding', never on 'skipped': {result}"
    )


# --------------------------------------------------------------------------- #
# 4. ruff comes from the pipeline's environment, not the target repo.
# --------------------------------------------------------------------------- #
def test_ingest_ignores_target_repo_venv_ruff_version(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """repo_root is the TARGET project; its .venv ruff must not be used."""
    target_repo = tmp_path / "target_repo"
    target_repo.mkdir()
    _write_script(target_repo / ".venv" / "bin" / "ruff", FAKE_RUFF_OLD_VERSION)
    pipeline_ruff = _write_script(tmp_path / "fakebin" / "ruff", FAKE_RUFF_CLEAN)
    _use_ruff(monkeypatch, pipeline_ruff)
    _write_plan(
        plan_dir,
        "targetruff",
        [_story("S1", [_py_entry(CLEAN_SOURCE)])],
        repo_root=target_repo,
    )
    _patch_plan_dir(monkeypatch, plan_dir)

    result = _ingest("targetruff")

    assert result["ok"] is True, (
        "a target repo whose .venv pins a different ruff failed the ingest of "
        f"a clean plan: {result}"
    )


# --------------------------------------------------------------------------- #
# 5. Lint runs upfront, before any Plane side effect.
# --------------------------------------------------------------------------- #
def test_lint_runs_before_any_plane_side_effect(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rejected plan must leave no orphaned epics/stories behind."""
    ruff = _write_script(tmp_path / "fakebin" / "ruff", FAKE_RUFF_REPORTS_F841)
    _use_ruff(monkeypatch, ruff)
    provider = _RecordingProvider()
    monkeypatch.setattr(p, "get_ticket_provider", lambda: provider)
    _write_plan(
        plan_dir,
        "ordering",
        [
            _story("S1", [_py_entry(CLEAN_SOURCE)]),
            _story("S2", [_py_entry(F841_SOURCE)]),
            _story("S3", [_py_entry(CLEAN_SOURCE)]),
        ],
    )
    _patch_plan_dir(monkeypatch, plan_dir)

    result = _ingest("ordering")

    assert result["ok"] is False, result
    assert provider.calls == [], (
        "the fixture lint ran AFTER Plane side effects: "
        f"{provider.calls} were created before the plan was rejected"
    )


# --------------------------------------------------------------------------- #
# 6. Regression guards: the classes the gate was written for still reject, and
#    clean / non-.py entries still pass.
# --------------------------------------------------------------------------- #
def test_ingest_rejects_f841_unused_local(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ruff = _write_script(tmp_path / "fakebin" / "ruff", FAKE_RUFF_REPORTS_F841)
    _use_ruff(monkeypatch, ruff)
    _write_plan(plan_dir, "f841", [_story("S1", [_py_entry(F841_SOURCE)])])
    _patch_plan_dir(monkeypatch, plan_dir)

    result = _ingest("f841")

    assert result["ok"] is False, result
    assert "F841" in result["error"], result


@requires_real_ruff
def test_ingest_rejects_syntax_error_fixture(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_plan(plan_dir, "e9", [_story("S1", [_py_entry(SYNTAX_ERROR_SOURCE)])])
    _patch_plan_dir(monkeypatch, plan_dir)

    result = _ingest("e9")

    assert result["ok"] is False, result


def test_ingest_accepts_clean_fixture(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ruff = _write_script(tmp_path / "fakebin" / "ruff", FAKE_RUFF_CLEAN)
    _use_ruff(monkeypatch, ruff)
    _write_plan(plan_dir, "clean", [_story("S1", [_py_entry(CLEAN_SOURCE)])])
    _patch_plan_dir(monkeypatch, plan_dir)

    result = _ingest("clean")

    assert result["ok"] is True, result


def test_ingest_ignores_non_py_entries(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_plan(
        plan_dir,
        "nonpy",
        [_story("S1", [_py_entry(CLEAN_SOURCE, "docs/spec.md")])],
    )
    _patch_plan_dir(monkeypatch, plan_dir)

    result = _ingest("nonpy")

    assert result["ok"] is True, result
