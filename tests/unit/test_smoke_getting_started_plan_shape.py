"""Plan-shape tests for ``build_smoke_plan`` in scripts/smoke_getting_started.py.

The smoke script used to build its 1-epic/1-story plan dict inline inside
``run_smoke()``. That inline plan carried an ``"acceptance"`` key holding two
plain criteria *strings*::

    "acceptance": [
        "README.md contains the smoke line",
        "no other file changed",
    ],

Per .claude/rules/pipeline-story-schema.md ``acceptance`` is an array of
``{path, source}`` FILE FIXTURES, not criteria strings - criteria belong in
``agent_instructions``. The stray strings make ``ingest_plan`` crash.

The story brief therefore requires:

(a) the plan dict is extracted into a NEW module-level function
    ``build_smoke_plan(repo_root: str) -> dict`` which ``run_smoke()`` calls
    (pure refactor: the plan's content must not otherwise change), so the
    plan's shape can be asserted without running the whole pipeline; and
(b) the ``"acceptance"`` key is DELETED from that plan, with its two criteria
    folded into the existing ``agent_instructions`` string (the existing text
    is KEPT and the criteria appended - not rewritten).

These tests are expected to be RED (ImportError / AttributeError / assertion
failure) until that refactor lands. They never run the pipeline and never
touch the network or the ``claude`` CLI.
"""

import importlib.util
import inspect
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "smoke_getting_started.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The two criteria that must move out of "acceptance" and into
# agent_instructions (verbatim, per the story brief).
CRITERION_README = "README.md contains the smoke line"
CRITERION_NO_OTHER_FILE = "no other file changed"
# Pre-existing agent_instructions text that must be KEPT (not rewritten).
EXISTING_INSTRUCTION_FRAGMENT = "Append exactly one new line to README.md"


# --------------------------------------------------------------------------
# helpers (loader copied from tests/unit/test_smoke_getting_started.py)
# --------------------------------------------------------------------------
def _load_script():
    """Import scripts/smoke_getting_started.py as a module, or fail loudly."""
    if not SCRIPT_PATH.exists():
        pytest.fail(
            f"scripts/smoke_getting_started.py not found at {SCRIPT_PATH}. "
            "Create it (stdlib + repo imports only, <=3 new functions: "
            "_prepare_scratch_env, _require_claude_backend, run_smoke/main)."
        )
    mod_name = "smoke_getting_started_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _build_plan(repo_root: str = "/tmp/x") -> dict:
    """Call build_smoke_plan, failing loudly (not with AttributeError) if absent."""
    module = _load_script()
    builder = getattr(module, "build_smoke_plan", None)
    if builder is None:
        pytest.fail(
            "scripts/smoke_getting_started.py must expose a module-level "
            "build_smoke_plan(repo_root: str) -> dict (the plan dict was "
            "extracted out of run_smoke())."
        )
    return builder(repo_root)


def _only_story(plan: dict) -> dict:
    """Return the single story of the single epic, asserting the shape."""
    epics = plan["epics"]
    assert isinstance(epics, list)
    assert len(epics) == 1, f"expected exactly 1 epic, got {len(epics)}"
    stories = epics[0]["stories"]
    assert isinstance(stories, list)
    assert len(stories) == 1, f"expected exactly 1 story, got {len(stories)}"
    return stories[0]


# --------------------------------------------------------------------------
# (a) build_smoke_plan exists, is module-level, and substitutes repo_root
# --------------------------------------------------------------------------
def test_build_smoke_plan_is_a_module_level_function():
    module = _load_script()
    builder = getattr(module, "build_smoke_plan", None)
    assert builder is not None, (
        "scripts/smoke_getting_started.py must define a module-level "
        "build_smoke_plan(repo_root: str) -> dict"
    )
    assert inspect.isfunction(builder), "build_smoke_plan must be a function"
    assert builder.__module__ == module.__name__, (
        "build_smoke_plan must be defined at module level in "
        "scripts/smoke_getting_started.py"
    )


def test_build_smoke_plan_signature_takes_repo_root():
    module = _load_script()
    builder = getattr(module, "build_smoke_plan", None)
    assert builder is not None, "build_smoke_plan is missing"
    params = list(inspect.signature(builder).parameters)
    assert params[:1] == ["repo_root"], (
        f"build_smoke_plan's first parameter must be named 'repo_root', got {params}"
    )


def test_repo_root_is_substituted_into_the_plan():
    plan = _build_plan("/tmp/x")
    assert isinstance(plan, dict)
    assert plan["repo_root"] == "/tmp/x"


def test_repo_root_is_not_hardcoded():
    """A different repo_root must show up verbatim (no constant baked in)."""
    plan = _build_plan("/tmp/some-other-scratch-repo")
    assert plan["repo_root"] == "/tmp/some-other-scratch-repo"


def test_plan_is_json_serializable():
    """run_smoke feeds the plan straight to json.dumps for save_plan."""
    plan = _build_plan("/tmp/x")
    dumped = json.dumps(plan)
    assert json.loads(dumped)["repo_root"] == "/tmp/x"


def test_each_call_returns_a_fresh_dict():
    """Mutating one returned plan must not leak into the next call."""
    first = _build_plan("/tmp/x")
    first["epics"].clear()
    first["injected"] = True
    second = _build_plan("/tmp/x")
    assert second["epics"], "build_smoke_plan must not return a shared/global dict"
    assert "injected" not in second


# --------------------------------------------------------------------------
# (a) the plan's content is otherwise unchanged (pure refactor)
# --------------------------------------------------------------------------
def test_plan_has_exactly_one_epic_and_one_story():
    plan = _build_plan("/tmp/x")
    assert isinstance(plan.get("epics"), list)
    assert len(plan["epics"]) == 1
    assert isinstance(plan["epics"][0].get("stories"), list)
    assert len(plan["epics"][0]["stories"]) == 1


def test_story_keeps_its_identity_fields():
    module = _load_script()
    story = _only_story(_build_plan("/tmp/x"))
    assert story["key"] == module.STORY_KEY
    assert story["persona"] == "software-engineer"
    assert story["risk"] == "low"
    assert story["dependencies"] == []
    assert isinstance(story["summary"], str) and story["summary"].strip()
    assert isinstance(story["description"], str) and story["description"].strip()


def test_epic_summary_is_preserved():
    plan = _build_plan("/tmp/x")
    summary = plan["epics"][0]["summary"]
    assert isinstance(summary, str) and summary.strip()


# --------------------------------------------------------------------------
# (b) "acceptance" is gone; its criteria live in agent_instructions
# --------------------------------------------------------------------------
def test_story_has_no_acceptance_key():
    story = _only_story(_build_plan("/tmp/x"))
    assert "acceptance" not in story, (
        "the story must NOT carry an 'acceptance' key: acceptance is an array "
        "of {path, source} FILE FIXTURES, not criteria strings"
    )


def test_script_source_contains_no_acceptance_literal():
    """Mirrors the done criterion: grep -n '"acceptance"' <script> is empty."""
    source = SCRIPT_PATH.read_text()
    assert '"acceptance"' not in source, (
        'scripts/smoke_getting_started.py must not contain the literal '
        '"acceptance" anywhere (the key was deleted entirely)'
    )


def test_agent_instructions_is_a_non_empty_string_mentioning_readme():
    story = _only_story(_build_plan("/tmp/x"))
    instructions = story["agent_instructions"]
    assert isinstance(instructions, str), (
        f"agent_instructions must be a str, got {type(instructions).__name__}"
    )
    assert instructions.strip(), "agent_instructions must not be empty"
    assert "README.md" in instructions


def test_agent_instructions_keeps_existing_text_and_appends_criteria():
    """The pre-existing text is KEPT; the two criteria are appended."""
    story = _only_story(_build_plan("/tmp/x"))
    instructions = story["agent_instructions"]
    assert EXISTING_INSTRUCTION_FRAGMENT in instructions, (
        "the existing agent_instructions text must be kept, not rewritten"
    )
    assert CRITERION_README in instructions, (
        "the 'README.md contains the smoke line' criterion must be folded "
        "into agent_instructions"
    )
    assert CRITERION_NO_OTHER_FILE in instructions, (
        "the 'no other file changed' criterion must be folded into "
        "agent_instructions"
    )


# --------------------------------------------------------------------------
# (b) NEGATIVE / BOUNDARY: no acceptance entry may be a bare criteria string
# --------------------------------------------------------------------------
def test_no_acceptance_element_is_a_string():
    story = _only_story(_build_plan("/tmp/x"))
    acceptance = story.get("acceptance") or []
    assert isinstance(acceptance, list), (
        f"acceptance must be a list when present, got {type(acceptance).__name__}"
    )
    offenders = [entry for entry in acceptance if isinstance(entry, str)]
    assert not offenders, (
        "acceptance entries must be {path, source} fixtures, never criteria "
        f"strings; found {offenders!r}"
    )


def test_no_acceptance_element_is_a_string_for_other_repo_roots():
    """Boundary: the shape holds for any repo_root, not just the default."""
    for repo_root in ("", "/", "/tmp/x", "relative/repo"):
        story = _only_story(_build_plan(repo_root))
        acceptance = story.get("acceptance") or []
        assert not [e for e in acceptance if isinstance(e, str)], (
            f"acceptance must not hold criteria strings (repo_root={repo_root!r})"
        )


# --------------------------------------------------------------------------
# run_smoke() uses the extracted builder instead of an inline dict
# --------------------------------------------------------------------------
def test_run_smoke_calls_build_smoke_plan():
    module = _load_script()
    run_smoke = getattr(module, "run_smoke", None)
    assert run_smoke is not None, "run_smoke must still exist"
    source = inspect.getsource(run_smoke)
    assert "build_smoke_plan(" in source, (
        "run_smoke() must call build_smoke_plan(...) instead of building the "
        "plan dict inline"
    )
    assert '"acceptance"' not in source, (
        "run_smoke() must not build an 'acceptance' key inline"
    )
