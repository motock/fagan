"""TDD suite: escalation's git teardown must run in the PLAN's repo, not the
process-global ``REPO_ROOT``.

ROOT CAUSE
----------
``pipeline/escalation.py``'s ``_escalate_to_claude`` and
``_escalate_to_local_fallback_model`` run their git teardown with
``cwd=REPO_ROOT`` - the PROCESS-GLOBAL repo root read lazily from
``pipeline.server``. The installed LaunchAgent passes
``REPO_ROOT=/nonexistent-repo-root-set-per-plan-only`` (a sentinel meaning
"each plan carries its own repo_root"), and
``pipeline/advance.py::_advance_pipeline_locked`` runs ``run_triage_sweep``
BEFORE entering ``_scoped_repo_root(plan_name)``. So a triage ``escalate_model``
ruling reaches escalation while the sentinel is still the active global, and
every ``subprocess.run(..., cwd=REPO_ROOT)`` raises::

    FileNotFoundError: [Errno 2] No such file or directory:
    PosixPath('/nonexistent-repo-root-set-per-plan-only')

``pipeline/triage.py``'s ``except Exception`` turns that into a park
("escalate_model ruled but ladder exhausted: FileNotFoundError: ..."), so
escalation onto a stronger executor has been silently dead for every plan.

CONTRACT PINNED BY THIS FILE
----------------------------
* The plan's own ``manifest["repo_root"]`` is authoritative - plans share one
  PLAN_DIR but each belongs to a different repo (the same precedence
  ``pipeline.server._repo_root_for`` already applies to dispatch and merge).
* ``REPO_ROOT`` remains ONLY the documented fallback for manifests ingested
  before ``repo_root`` existed, and is never the primary source.
* A new module-level helper ``_escalation_repo_root(manifest) -> Path`` is the
  single place that resolves it, defined immediately before
  ``_escalate_to_claude``; ``REPO_ROOT`` is referenced nowhere else in the file.
* Both escalation functions resolve ``repo_root`` from the manifest right after
  the story lookup and use it for all THREE of their git calls (no
  ``cwd=REPO_ROOT`` survives).
* The survivor functions (``_escalate_review_to_claude``, ``_escalation_target``,
  ``_auto_escalation_enabled``) and the story bookkeeping / journal / manifest
  write are untouched.

Every test stubs the git boundary (``pipeline.escalation.subprocess``) and sets
the global sentinel explicitly, so nothing here asserts against this machine's
real configuration and no real git is ever shelled out.
"""

from __future__ import annotations

import ast
import json
import subprocess as real_subprocess
from pathlib import Path

import pytest

from pipeline import escalation as esc
from pipeline import server

SENTINEL = Path("/nonexistent-repo-root-set-per-plan-only")

# The three git calls each escalation function makes (worktree remove + the
# convention branch delete + the alias branch delete).
_EXPECTED_CWD_CALLS = 3


class _FakeSubprocess:
    """Stand-in for the ``subprocess`` module that records each call's cwd."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, "cwd": kwargs.get("cwd")})
        return real_subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def fake_git(monkeypatch):
    fake = _FakeSubprocess()
    monkeypatch.setattr(esc, "subprocess", fake)
    return fake


@pytest.fixture(autouse=True)
def _isolate_plan_dir(monkeypatch, tmp_path):
    """Keep the journal path and notifications out of this machine's real dirs.

    The escalation target is pinned via the escalation-specific env override so
    nothing here depends on this machine's real model registry.
    """
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(esc, "_notify_user", lambda *a, **k: None)
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", "claude")
    monkeypatch.delenv("PIPELINE_ESCALATION_MODEL", raising=False)


def _manifest(repo_root, story_key: str, **story_overrides) -> dict:
    """A failed local story, exactly the shape escalation is handed.

    ``worktree`` is deliberately empty: ``_resolve_story_branch`` then probes a
    non-existent cwd, fails open to the convention branch, and never spawns a
    real git process.
    """
    story = {
        "summary": "thing",
        "status": "failed",
        "pid": 4242,
        "worktree": "",
        "backend": "local",
        "model": "gemma4:26b-a4b-it-qat",
        "dispatch_attempts": 1,
        "dispatch_error": "boom",
        "step_cap_streak": 2,
        "infra_failure_streak": 1,
    }
    story.update(story_overrides)
    manifest: dict = {"epics": {}, "stories": {story_key: story}}
    if repo_root is not None:
        manifest["repo_root"] = repo_root
    return manifest


def _cwds(fake_git) -> set[str]:
    return {str(call["cwd"]) for call in fake_git.calls}


def _module_source() -> str:
    return Path(esc.__file__).read_text(encoding="utf-8")


def _function_segment(name: str) -> str:
    src = _module_source()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(
        f"pipeline/escalation.py has no module-level function {name!r}"
    )


def _helper():
    fn = getattr(esc, "_escalation_repo_root", None)
    assert fn is not None, (
        "pipeline.escalation._escalation_repo_root(manifest) -> Path is missing: "
        "escalation must resolve its teardown repo from the plan's own "
        "manifest['repo_root'] (falling back to the process-global REPO_ROOT) "
        "instead of using REPO_ROOT for every git call"
    )
    return fn


# ---------------------------------------------------------------------------
# 1. POSITIVE: the plan's repo_root wins over the sentinel global
# ---------------------------------------------------------------------------


def test_claude_escalation_uses_the_plan_repo_root_not_the_sentinel_global(
    fake_git, monkeypatch, tmp_path
):
    monkeypatch.setattr(server, "REPO_ROOT", SENTINEL)
    manifest = _manifest(str(tmp_path), "S1")
    story = manifest["stories"]["S1"]
    manifest_path = tmp_path / "plan.S1.manifest.json"

    esc._escalate_to_claude(manifest, "plan", "S1", manifest_path)

    assert fake_git.calls, "escalation must still run its git teardown"
    assert _cwds(fake_git) == {str(tmp_path)}, (
        "every escalation git call must run in the plan's own repo_root, not the "
        "process-global sentinel"
    )
    assert SENTINEL not in {call["cwd"] for call in fake_git.calls}
    assert story["status"] == "todo"
    assert story["escalated"] is True


def test_fallback_model_escalation_uses_the_plan_repo_root_not_the_sentinel_global(
    fake_git, monkeypatch, tmp_path
):
    monkeypatch.setattr(server, "REPO_ROOT", SENTINEL)
    manifest = _manifest(str(tmp_path), "S2")
    story = manifest["stories"]["S2"]
    manifest_path = tmp_path / "plan.S2.manifest.json"

    esc._escalate_to_local_fallback_model(
        manifest, "plan", "S2", manifest_path, "gpt-oss-20b-high:latest"
    )

    assert fake_git.calls, "escalation must still run its git teardown"
    assert _cwds(fake_git) == {str(tmp_path)}, (
        "the fallback-model teardown must run in the plan's own repo_root too"
    )
    assert story["status"] == "todo"
    assert story["tried_fallback_model"] is True
    assert story["model"] == "gpt-oss-20b-high:latest"


# ---------------------------------------------------------------------------
# 2. NEGATIVE / BOUNDARY: the documented fallback survives
# ---------------------------------------------------------------------------


def test_legacy_manifest_without_repo_root_uses_the_process_global(
    fake_git, monkeypatch, tmp_path
):
    """Manifests ingested before repo_root existed must keep working."""
    monkeypatch.setattr(server, "REPO_ROOT", tmp_path)
    manifest = _manifest(None, "S3")
    assert "repo_root" not in manifest

    esc._escalate_to_claude(manifest, "plan", "S3", tmp_path / "plan.S3.manifest.json")

    assert fake_git.calls
    assert _cwds(fake_git) == {str(tmp_path)}, (
        "with no manifest repo_root the process-global REPO_ROOT is the "
        "documented fallback and must still be used"
    )


def test_empty_repo_root_falls_back_and_never_yields_an_empty_cwd(
    fake_git, monkeypatch, tmp_path
):
    monkeypatch.setattr(server, "REPO_ROOT", tmp_path)
    manifest = _manifest("", "S4")

    esc._escalate_to_claude(manifest, "plan", "S4", tmp_path / "plan.S4.manifest.json")

    assert fake_git.calls
    cwds = _cwds(fake_git)
    assert cwds == {str(tmp_path)}, (
        "an empty repo_root must fall back to the global, not be used verbatim"
    )
    assert "" not in cwds
    assert "None" not in cwds


# ---------------------------------------------------------------------------
# 3. The helper itself: precedence, laziness, and shape
# ---------------------------------------------------------------------------


def test_helper_prefers_the_manifest_repo_root(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "REPO_ROOT", SENTINEL)

    result = _helper()({"repo_root": str(tmp_path)})

    assert isinstance(result, Path)
    assert result == tmp_path


def test_helper_falls_back_to_the_process_global(monkeypatch, tmp_path):
    """Absent / empty / None repo_root all degrade to the global, never crash."""
    monkeypatch.setattr(server, "REPO_ROOT", tmp_path)
    helper = _helper()

    assert helper({}) == tmp_path
    assert helper({"repo_root": ""}) == tmp_path
    assert helper({"repo_root": None}) == tmp_path


def test_helper_reads_repo_root_lazily_from_the_server_module(monkeypatch, tmp_path):
    """Patching ``server.REPO_ROOT`` must be honoured (lazy import, not a copy)."""
    monkeypatch.setattr(server, "REPO_ROOT", tmp_path)

    assert _helper()({}) == tmp_path


def test_helper_has_a_docstring_naming_the_contract():
    doc = _helper().__doc__ or ""
    assert doc.strip(), "_escalation_repo_root must document its precedence"
    assert "repo_root" in doc
    assert "REPO_ROOT" in doc


# ---------------------------------------------------------------------------
# 4. MECHANICAL: the anchored edits the brief authorizes
# ---------------------------------------------------------------------------


def test_helper_is_defined_immediately_before_escalate_to_claude():
    tree = ast.parse(_module_source())
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert "_escalation_repo_root" in names, (
        "the helper must be a module-level function in pipeline/escalation.py"
    )
    index = names.index("_escalation_repo_root")
    assert names[index + 1] == "_escalate_to_claude", (
        "_escalation_repo_root must be defined immediately BEFORE "
        "_escalate_to_claude"
    )


def test_no_git_call_still_uses_the_process_global_repo_root():
    src = _module_source()
    assert "cwd=REPO_ROOT" not in src, (
        "every escalation git call must use the plan-resolved repo_root; no "
        "cwd=REPO_ROOT may survive"
    )
    assert src.count("cwd=repo_root") == 6, (
        "both escalation functions make three git calls each, all of which must "
        "now use cwd=repo_root"
    )


def test_repo_root_is_referenced_only_inside_the_helper():
    """DONE CRITERIA: ``grep -n "REPO_ROOT" pipeline/escalation.py`` shows it
    only inside ``_escalation_repo_root``.

    Graded both literally (every line mentioning REPO_ROOT must fall inside the
    helper's span - so the stale module docstring must be updated too) and at
    the AST level (no code outside the helper may read the global).
    """
    src = _module_source()
    tree = ast.parse(src)
    helper = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_escalation_repo_root"
    )
    start, end = helper.lineno, helper.end_lineno

    literal_offenders = [
        (lineno, line.strip())
        for lineno, line in enumerate(src.splitlines(), start=1)
        if "REPO_ROOT" in line and not (start <= lineno <= end)
    ]
    assert not literal_offenders, (
        "REPO_ROOT must appear only inside _escalation_repo_root; still found at "
        f"{literal_offenders} (update the stale module docstring if that is the "
        "only remaining mention)"
    )

    refs = [
        node.lineno
        for node in ast.walk(tree)
        if (isinstance(node, ast.Name) and node.id == "REPO_ROOT")
        or (isinstance(node, ast.Attribute) and node.attr == "REPO_ROOT")
    ]
    assert refs, "the helper must actually read REPO_ROOT as its fallback"
    assert all(start <= lineno <= end for lineno in refs), (
        f"REPO_ROOT code references outside the helper: {refs}"
    )


@pytest.mark.parametrize(
    "name", ["_escalate_to_claude", "_escalate_to_local_fallback_model"]
)
def test_each_escalation_function_resolves_and_uses_the_plan_repo_root(name):
    segment = _function_segment(name)

    assert "from .server import PLAN_DIR, REPO_ROOT" not in segment, (
        "REPO_ROOT now comes from the helper; importing it here is an "
        "unused-import lint error"
    )
    assert "from .server import PLAN_DIR" in segment, (
        "PLAN_DIR is still needed for the journal path"
    )
    assert "_escalation_repo_root(manifest)" in segment, (
        "the function must resolve repo_root from the manifest via the helper"
    )
    assert segment.count("cwd=repo_root") == _EXPECTED_CWD_CALLS, (
        f"{name} makes three git calls; all three must use cwd=repo_root"
    )
    assert "cwd=REPO_ROOT" not in segment
    assert "journal_path = PLAN_DIR /" in segment, (
        "the journal path must keep using PLAN_DIR"
    )
    assert "_atomic_write_json(manifest_path, manifest)" in segment, (
        "the manifest write must survive"
    )


@pytest.mark.parametrize(
    "name", ["_escalate_to_claude", "_escalate_to_local_fallback_model"]
)
def test_repo_root_assignment_follows_the_story_lookup(name):
    lines = _function_segment(name).splitlines()
    lookup = next(
        i
        for i, line in enumerate(lines)
        if line.strip() == 'story = manifest["stories"][story_key]'
    )
    following = [
        line.strip()
        for line in lines[lookup + 1 :]
        if line.strip() and not line.strip().startswith("#")
    ]
    assert following, f"{name} must do something after the story lookup"
    assert following[0] == "repo_root = _escalation_repo_root(manifest)", (
        "repo_root must be resolved immediately after the story lookup, before "
        "any git call"
    )


def test_survivor_functions_are_untouched():
    for name in (
        "_escalate_review_to_claude",
        "_escalation_target",
        "_auto_escalation_enabled",
    ):
        assert callable(getattr(esc, name, None)), f"{name} must survive"

    review = _function_segment("_escalate_review_to_claude")
    assert "repo_root" not in review, (
        "_escalate_review_to_claude is on the do-not-touch list"
    )
    assert "cwd=" not in review


def test_the_fix_lives_only_in_escalation():
    """The helper must not be pushed out into the callers.

    ``pipeline/advance.py``, ``pipeline/triage.py``, ``pipeline/server.py`` and
    ``pipeline/pr.py`` are on the do-not-touch list: resolving the repo inside
    escalation covers every caller (triage's sweep, check_story_status) at once.
    """
    pipeline_dir = Path(esc.__file__).parent
    offenders = sorted(
        path.name
        for path in pipeline_dir.glob("*.py")
        if path.name != "escalation.py"
        and "_escalation_repo_root" in path.read_text(encoding="utf-8")
    )
    assert not offenders, (
        "_escalation_repo_root must be defined and used only in "
        f"pipeline/escalation.py; also found in {offenders}"
    )


# ---------------------------------------------------------------------------
# 5. The bookkeeping the brief says to leave alone still happens
# ---------------------------------------------------------------------------


def test_claude_escalation_keeps_its_bookkeeping(fake_git, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "REPO_ROOT", SENTINEL)
    manifest = _manifest(str(tmp_path), "S5")
    story = manifest["stories"]["S5"]
    manifest_path = tmp_path / "plan.S5.manifest.json"

    esc._escalate_to_claude(manifest, "plan", "S5", manifest_path)

    for key in ("pid", "worktree", "dispatch_attempts", "dispatch_error"):
        assert key not in story, f"{key} must still be popped by the teardown"
    assert manifest_path.exists(), "the manifest must still be persisted"
    written = json.loads(manifest_path.read_text())
    assert written["stories"]["S5"]["status"] == "todo"
    assert written["stories"]["S5"]["escalated"] is True


def test_fallback_escalation_keeps_its_bookkeeping(fake_git, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "REPO_ROOT", SENTINEL)
    manifest = _manifest(str(tmp_path), "S6")
    story = manifest["stories"]["S6"]
    manifest_path = tmp_path / "plan.S6.manifest.json"

    esc._escalate_to_local_fallback_model(
        manifest, "plan", "S6", manifest_path, "gpt-oss-20b-high:latest"
    )

    for key in ("pid", "worktree", "dispatch_attempts", "dispatch_error"):
        assert key not in story
    assert manifest_path.exists()
    written = json.loads(manifest_path.read_text())
    assert written["stories"]["S6"]["tried_fallback_model"] is True
