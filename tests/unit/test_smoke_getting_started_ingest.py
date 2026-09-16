"""SMOKE-3: the getting-started smoke plan must survive the REAL ingest path.

WHY THIS FILE EXISTS
--------------------
``scripts/smoke_getting_started.py`` landed 2026-09-02. The acceptance-fixture
lint validator landed 2026-09-03 and broke the smoke script's ingest with an
uncaught ``AttributeError`` (``'str' object has no attribute 'get'``): the
script's plan carried two plain criteria *strings* in the story's
``acceptance`` field, but per ``.claude/rules/pipeline-story-schema.md``
``acceptance`` is an array of ``{path, source}`` FILE FIXTURES. Nobody noticed
for 12 days, because every existing test of the smoke script MOCKS the
pipeline - nothing ever put the script's real plan through the real ingest
path.

This module closes that hole: it builds the script's REAL plan and drives it
through the REAL ``pipeline.server.save_plan`` / ``pipeline.server.ingest_plan``
against a SCRATCH ``PLAN_DIR``, so a future change to ingest validation cannot
silently break the documented getting-started path again.

Two halves, both required:

* the happy path - the real plan ingests cleanly (``ok is True``) and the
  resulting manifest holds exactly one story; and
* the NEGATIVE CONTROL - the same plan with the story's ``acceptance`` set to
  ``["a criterion string"]`` must make ``ingest_plan`` RETURN
  ``{"ok": False, ...}`` rather than raise. That is precisely what regressed
  (SMOKE-1), so this assertion is the guard.

SCRATCH PLAN_DIR WIRING (do not weaken)
---------------------------------------
``pipeline.paths`` reads ``PLAN_DIR`` at IMPORT time and several modules each
hold their own binding, so patching only the environment variable is NOT
enough: it would silently flock the operator's REAL ``~/.claude/plans`` and can
produce false terminal states in the live pipeline. Every test here therefore
uses the shared conftest ``plan_dir`` fixture (which patches
``pipeline.server``, ``pipeline.persistence`` and ``pipeline.concurrency``) and
re-pins all three bindings explicitly via :func:`_pin_plan_dir` - the proven
pattern from ``tests/unit/test_ingest_fixture_lint.py``. The happy-path test
additionally asserts the operator's real plan directory gains no file named
after this plan.

NOTE ON ``build_smoke_plan``: the story brief expected a module-level
``build_smoke_plan(repo_root)`` helper from SMOKE-2. SMOKE-2 was re-scoped
during review - the script's footprint guard
(``tests/unit/test_smoke_getting_started.py::
test_at_most_the_three_allowed_top_level_functions``) pins the script's
top-level defs, so the plan dict lives INLINE inside ``run_smoke()``. The
:func:`_build_smoke_plan` helper below therefore prefers a module-level
``build_smoke_plan`` if one ever exists and otherwise extracts the inline
``plan = {...}`` literal from ``run_smoke``'s source (ast + ``literal_eval``,
the technique ``tests/unit/test_smoke_getting_started_plan_shape.py`` uses).
Either way it is the script's REAL plan - never a hand-copied fixture.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline import server as p

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "smoke_getting_started.py"

# The script's own constants - the plan this test drives must be the script's.
PLAN_NAME = "smoke-getting-started"
STORY_KEY = "S1"

# The documented common mistake that crashed ingest for 12 days: a criteria
# string where a {"path": ..., "source": ...} fixture dict belongs.
CRITERION_STRING = "a criterion string"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _load_script():
    """Import scripts/smoke_getting_started.py as a module, or fail loudly."""
    if not SCRIPT_PATH.exists():
        pytest.fail(f"scripts/smoke_getting_started.py not found at {SCRIPT_PATH}")
    mod_name = "smoke_getting_started_ingest_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class _ResolvePlanNames(ast.NodeTransformer):
    """Rewrite the plan literal's dynamic names to Constants.

    ``ast.literal_eval`` rejects Call/Name nodes, so ``str(target_repo)``
    becomes a Constant of *repo_root* (the same substitution ``run_smoke``
    performs) and ``STORY_KEY`` becomes a Constant of the script's STORY_KEY.
    """

    def __init__(self, repo_root: str, story_key: str) -> None:
        self._repo_root = repo_root
        self._story_key = story_key

    def visit_Call(self, node: ast.Call) -> ast.expr:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "str"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "target_repo"
        ):
            return ast.copy_location(ast.Constant(value=self._repo_root), node)
        return self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> ast.expr:
        if node.id == "STORY_KEY":
            return ast.copy_location(ast.Constant(value=self._story_key), node)
        return node


def _build_smoke_plan(repo_root: str) -> dict:
    """Return the smoke script's REAL plan for *repo_root*.

    Prefers a module-level ``build_smoke_plan`` (the brief's expectation) and
    falls back to extracting the inline ``plan = {...}`` literal from
    ``run_smoke``'s source - see the module docstring. Never runs the pipeline,
    never touches the network or the ``claude`` CLI.
    """
    module = _load_script()
    builder = getattr(module, "build_smoke_plan", None)
    if callable(builder):
        return builder(repo_root)

    tree = ast.parse(inspect.getsource(module.run_smoke))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "plan" for t in node.targets)
            and isinstance(node.value, ast.Dict)
        ):
            return ast.literal_eval(
                _ResolvePlanNames(repo_root, module.STORY_KEY).visit(node.value)
            )
    pytest.fail(
        "could not find the inline `plan = {...}` dict in run_smoke() and the "
        "script has no module-level build_smoke_plan(); the plan literal's "
        "shape drifted - re-read scripts/smoke_getting_started.py"
    )


def _scratch_repo(tmp_path: Path) -> Path:
    """Create a real git repo with one commit under *tmp_path*.

    ``ingest_plan`` validates that ``repo_root`` is an existing directory, and
    the smoke path is a git-repo path, so the scratch target must be a real
    repo with at least one commit.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# scratch target repo\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "smoke",
        "GIT_AUTHOR_EMAIL": "smoke@example.invalid",
        "GIT_COMMITTER_NAME": "smoke",
        "GIT_COMMITTER_EMAIL": "smoke@example.invalid",
    }
    for args in (
        ["git", "init", "-q"],
        ["git", "add", "README.md"],
        ["git", "commit", "-q", "-m", "scratch: initial commit"],
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True, env=env)
    return repo


def _pin_plan_dir(monkeypatch: pytest.MonkeyPatch, plan_dir: Path) -> None:
    """Patch ALL THREE PLAN_DIR bindings to *plan_dir*.

    Mirrors ``tests/unit/test_ingest_fixture_lint.py``: patching only the env
    var (or only ``pipeline.server``) would let a module keep the operator's
    real ``~/.claude/plans`` binding and flock it.
    """
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers

    monkeypatch.setattr(p, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(ppers, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(pcon, "PLAN_DIR", plan_dir)


def _real_plan_dir() -> Path:
    return Path.home() / ".claude" / "plans"


def _smoke_named_entries(directory: Path) -> set[str]:
    """Names in *directory* that belong to THIS plan (delta-safe snapshot).

    The ``"."`` suffix keeps unrelated plans whose name merely starts with
    ``smoke-getting-started`` (e.g. the pre-existing
    ``smoke-getting-started-repair`` plan) out of the snapshot, so a live
    pipeline touching those cannot make this assertion flake.
    """
    if not directory.is_dir():
        return set()
    return {n for n in os.listdir(directory) if n.startswith(f"{PLAN_NAME}.")}


def _save_and_ingest(plan: dict) -> tuple[dict, dict]:
    """Drive the REAL server entry points the smoke script drives."""
    saved = p.save_plan(PLAN_NAME, json.dumps(plan))
    assert saved.get("ok") is True, f"save_plan failed: {saved}"
    return saved, p.ingest_plan(PLAN_NAME)


def _only_story(plan: dict) -> dict:
    epics = plan["epics"]
    assert isinstance(epics, list) and len(epics) == 1, f"expected 1 epic: {epics}"
    stories = epics[0]["stories"]
    assert isinstance(stories, list) and len(stories) == 1, f"expected 1 story: {stories}"
    return stories[0]


# --------------------------------------------------------------------------- #
# 1. The plan this test drives is the script's REAL plan.
# --------------------------------------------------------------------------- #
def test_build_smoke_plan_returns_the_scripts_real_plan(tmp_path: Path) -> None:
    """The plan under test is the script's own, not a hand-copied fixture."""
    repo = _scratch_repo(tmp_path)
    plan = _build_smoke_plan(str(repo))

    assert plan["repo_root"] == str(repo), plan["repo_root"]
    story = _only_story(plan)
    assert story["key"] == STORY_KEY, story["key"]
    assert story["summary"], "the smoke story must carry a summary"
    assert story["agent_instructions"], "the smoke story must carry instructions"


def test_smoke_plan_acceptance_is_well_formed_fixture_dicts(tmp_path: Path) -> None:
    """SMOKE-2's invariant: acceptance is absent or a list of {path, source} dicts.

    A criteria *string* here is exactly what crashed ingest for 12 days.
    """
    plan = _build_smoke_plan(str(_scratch_repo(tmp_path)))
    story = _only_story(plan)

    acceptance = story.get("acceptance")
    if acceptance is None:
        return
    assert isinstance(acceptance, list), f"acceptance must be a list: {acceptance!r}"
    for i, entry in enumerate(acceptance):
        assert isinstance(entry, dict), (
            f"acceptance[{i}] is {type(entry).__name__} ({entry!r}); acceptance "
            "entries must be {'path': ..., 'source': ...} dicts, never criteria "
            "strings (the SMOKE-1/SMOKE-2 regression)"
        )


def test_smoke_script_drives_the_same_save_and_ingest_entry_points() -> None:
    """The script really calls the two server entry points this test drives."""
    source = SCRIPT_PATH.read_text()
    assert "save_plan(PLAN_NAME" in source, (
        "the smoke script no longer calls save_plan(PLAN_NAME, ...); this test "
        "would be driving a path the script does not use"
    )
    assert "ingest_plan(PLAN_NAME" in source, (
        "the smoke script no longer calls ingest_plan(PLAN_NAME); this test "
        "would be driving a path the script does not use"
    )


# --------------------------------------------------------------------------- #
# 2. Happy path: the real plan ingests cleanly through the real pipeline.
# --------------------------------------------------------------------------- #
def test_smoke_plan_ingests_cleanly_through_the_real_pipeline(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """save_plan + ingest_plan on the real plan: ok True, exactly one story."""
    _pin_plan_dir(monkeypatch, plan_dir)
    repo = _scratch_repo(tmp_path)
    real_dir = _real_plan_dir()
    before = _smoke_named_entries(real_dir)

    plan = _build_smoke_plan(str(repo))
    _saved, ingested = _save_and_ingest(plan)

    assert ingested.get("ok") is True, (
        "the getting-started smoke plan no longer ingests cleanly - the "
        f"documented getting-started path is broken: {ingested}"
    )

    manifest_path = plan_dir / f"{PLAN_NAME}.manifest.json"
    assert manifest_path.exists(), f"no manifest written to the scratch dir: {plan_dir}"
    assert Path(ingested["manifest_path"]) == manifest_path, ingested["manifest_path"]

    manifest = json.loads(manifest_path.read_text())
    stories = manifest["stories"]
    assert len(stories) == 1, f"expected exactly one story, got {sorted(stories)}"
    assert STORY_KEY in stories, f"expected story key {STORY_KEY!r}: {sorted(stories)}"
    assert stories[STORY_KEY]["summary"] == _only_story(plan)["summary"]
    assert manifest["repo_root"] == str(repo)

    # The scratch wiring held: the operator's real plan dir gained nothing.
    assert _smoke_named_entries(real_dir) == before, (
        "the ingest wrote into the operator's REAL plan dir "
        f"({real_dir}); the scratch PLAN_DIR wiring did not hold"
    )


def test_scratch_plan_dir_wiring_pins_all_three_bindings(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All three PLAN_DIR bindings point at the scratch dir, never the real one."""
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers

    _pin_plan_dir(monkeypatch, plan_dir)

    assert p.PLAN_DIR == plan_dir
    assert ppers.PLAN_DIR == plan_dir
    assert pcon.PLAN_DIR == plan_dir

    real_dir = _real_plan_dir()
    assert plan_dir != real_dir, "the scratch plan dir IS the operator's real one"
    assert real_dir not in plan_dir.parents, f"{plan_dir} lives under {real_dir}"
    assert tmp_path in plan_dir.parents, f"{plan_dir} is not under tmp_path"

    # The store resolves PLAN_DIR through pipeline.server, so the manifest the
    # ingest writes must land in the scratch dir too.
    assert p._store.manifest_path(PLAN_NAME).parent == plan_dir


def test_scratch_target_repo_is_a_real_git_repo_with_a_commit(tmp_path: Path) -> None:
    """ingest_plan requires an existing repo_root; the smoke path needs git."""
    repo = _scratch_repo(tmp_path)
    assert repo.is_dir()
    assert (repo / ".git").is_dir(), "the scratch target repo is not a git repo"
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert log.stdout.strip(), "the scratch target repo has no commit"


# --------------------------------------------------------------------------- #
# 3. NEGATIVE CONTROL: the exact regression. Must RETURN, never raise.
# --------------------------------------------------------------------------- #
def test_negative_control_criteria_string_returns_ok_false_not_raises(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """acceptance = ["a criterion string"] -> {"ok": False, ...}, no traceback.

    This is the guard: before SMOKE-1 this raised
    ``AttributeError: 'str' object has no attribute 'get'`` out of ingest.
    """
    _pin_plan_dir(monkeypatch, plan_dir)
    plan = _build_smoke_plan(str(_scratch_repo(tmp_path)))
    story = _only_story(plan)
    story["acceptance"] = [CRITERION_STRING]

    _saved, result = _save_and_ingest(plan)

    assert isinstance(result, dict), f"ingest_plan must return a dict: {result!r}"
    assert result.get("ok") is False, (
        "a criteria string in the acceptance field was ACCEPTED (or raised); "
        f"ingest must reject it with ok False: {result}"
    )
    error = result.get("error") or ""
    assert "malformed acceptance" in error, error
    assert "acceptance[0]" in error, error
    assert "str" in error, error
    assert CRITERION_STRING in error, error

    # Rejected upfront, before any Plane side effect: no orphaned manifest.
    assert not (plan_dir / f"{PLAN_NAME}.manifest.json").exists(), (
        "the rejected ingest still wrote a manifest (orphaned side effect)"
    )


def test_negative_control_acceptance_that_is_not_a_list_returns_ok_false(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bare string (not a list at all) is also a returned rejection."""
    _pin_plan_dir(monkeypatch, plan_dir)
    plan = _build_smoke_plan(str(_scratch_repo(tmp_path)))
    _only_story(plan)["acceptance"] = CRITERION_STRING

    _saved, result = _save_and_ingest(plan)

    assert result.get("ok") is False, result
    error = result.get("error") or ""
    assert "malformed acceptance" in error, error
    assert "must be a list" in error, error


def test_negative_control_mixed_entries_returns_ok_false(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A well-formed dict followed by a criteria string is still rejected."""
    _pin_plan_dir(monkeypatch, plan_dir)
    plan = _build_smoke_plan(str(_scratch_repo(tmp_path)))
    _only_story(plan)["acceptance"] = [
        {"path": "tests/test_smoke_ok.py", "source": "def test_ok():\n    assert True\n"},
        CRITERION_STRING,
    ]

    _saved, result = _save_and_ingest(plan)

    assert result.get("ok") is False, result
    error = result.get("error") or ""
    assert "malformed acceptance" in error, error
    assert "acceptance[1]" in error, error


# --------------------------------------------------------------------------- #
# 4. Boundary cases on the ingest path itself.
# --------------------------------------------------------------------------- #
def test_ingest_rejects_a_repo_root_that_is_not_a_directory(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing repo_root is a returned rejection, not an exception."""
    _pin_plan_dir(monkeypatch, plan_dir)
    plan = _build_smoke_plan(str(tmp_path / "does-not-exist"))

    _saved, result = _save_and_ingest(plan)

    assert result.get("ok") is False, result
    assert "repo_root" in (result.get("error") or ""), result


def test_ingest_rejects_a_missing_plan(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingesting a plan that was never saved is a returned rejection."""
    _pin_plan_dir(monkeypatch, plan_dir)

    result = p.ingest_plan(PLAN_NAME)

    assert result.get("ok") is False, result
    assert "No plan named" in (result.get("error") or ""), result
