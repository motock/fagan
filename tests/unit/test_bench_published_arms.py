"""Contract for the published benchmark matrix arms (tests/benchmark/models.py).

The published matrix compares three routing arms. Role resolution priority is
plan role_config -> registry roles.* -> PIPELINE_BACKEND_<ROLE> env
(app/role_registry.resolve_role), and the live registry pins every role to
claude/sonnet, so an arm that sets only PIPELINE_* env vars silently runs
Claude. Every published arm therefore pins its roles in the entry's
"role_config" and names its implementer in "story_model" (a friendly
providers.ollama.models registry name, never an Ollama tag).

MODELS is cumulative, so these tests assert only what the published-arms story
adds - never the dict's total size or its full key list. The other surviving
entries the story must not touch are a scope constraint on the implementer,
not an assertion here; only the one survivor the plan calls out by name
(gptoss_claude_review, the env-only control) is graded.
"""
import re
from pathlib import Path

import pytest

from tests.benchmark import models

# Published arm -> the friendly name its dispatch role is pinned to.
_NEW_ARMS = {
    "glm_claude_review": "glm",
    "gptoss_claude_review_s60": "gpt-oss",
    "gptoss_claude_review_s120": "gpt-oss",
}
_CLAUDE = {"provider": "claude", "model": "sonnet"}
# Ways a README Notes cell can say which roles Claude plays. Deliberately loose:
# the requirement is that the row names the roles, not the exact wording.
_ROLE_HINTS = (
    "planner", "test_author", "review", "overlord",
    "other role", "every role", "all role", "non-dispatch", "every other",
)


def _arm(name: str) -> dict:
    assert name in models.MODELS, f"MODELS must define the published arm {name!r}"
    return models.MODELS[name]


def _role_config(name: str) -> dict:
    arm = _arm(name)
    assert "role_config" in arm, f"the {name!r} arm must pin its roles in role_config"
    return arm["role_config"]


@pytest.mark.parametrize("arm_name", sorted(_NEW_ARMS))
def test_published_arm_is_a_real_cell(arm_name):
    assert _arm(arm_name)["mock"] is False


@pytest.mark.parametrize(("arm_name", "story_model"), sorted(_NEW_ARMS.items()))
def test_published_arm_pins_every_role(arm_name, story_model):
    roles = _role_config(arm_name)
    assert set(roles) == set(models._BENCH_ROLES)
    assert roles["dispatch"] == {"provider": "ollama", "model": story_model}
    for role in models._BENCH_ROLES:
        if role != "dispatch":
            assert roles[role] == _CLAUDE
    assert _arm(arm_name)["story_model"] == story_model


@pytest.mark.parametrize("arm_name", sorted(_NEW_ARMS))
def test_published_arm_timeout_and_no_review_env(arm_name):
    env = _arm(arm_name)["env"]
    # 1800s, not the shared 900s default: the default expires before a
    # 120-step run reaches its step cap.
    assert env["PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS"] == "1800"
    # role_config is the single source for review; an env pin would be a
    # second, conflicting source.
    assert "PIPELINE_BACKEND_REVIEW" not in env
    # The arm must dispatch the model it claims to test - a wrong tag here is
    # exactly the silent-misroute failure this story exists to prevent.
    expected_tag = {
        "glm_claude_review": "glm-5.3-flash:cloud",
        "gptoss_claude_review_s60": "gpt-oss:20b",
        "gptoss_claude_review_s120": "gpt-oss:20b",
    }[arm_name]
    assert env["PIPELINE_LOCAL_MODEL_DEFAULT"] == expected_tag


def test_step_budget_is_the_only_difference_between_the_two_gptoss_arms():
    s60 = _arm("gptoss_claude_review_s60")
    s120 = _arm("gptoss_claude_review_s120")
    assert s60["role_config"] == s120["role_config"]
    assert s60["story_model"] == s120["story_model"]
    assert s60["mock"] == s120["mock"]
    differing = {
        key
        for key in set(s60["env"]) | set(s120["env"])
        if s60["env"].get(key) != s120["env"].get(key)
    }
    assert differing == {"PIPELINE_LOCAL_MAX_STEPS"}
    assert s60["env"]["PIPELINE_LOCAL_MAX_STEPS"] == "60"
    assert s120["env"]["PIPELINE_LOCAL_MAX_STEPS"] == "120"


def test_published_role_config_without_a_dispatch_is_all_claude():
    roles = models._published_role_config()
    assert set(roles) == set(models._BENCH_ROLES)
    for role in models._BENCH_ROLES:
        assert roles[role] == _CLAUDE
    # Negative: no role may leak onto the arm's own backend.
    assert all(entry["provider"] == "claude" for entry in roles.values())


def test_published_role_config_pins_only_dispatch_to_the_arm_model():
    roles = models._published_role_config("glm")
    assert set(roles) == set(models._BENCH_ROLES)
    assert roles["dispatch"] == {"provider": "ollama", "model": "glm"}
    for role in models._BENCH_ROLES:
        if role != "dispatch":
            assert roles[role] == _CLAUDE


def test_sonnet_arm_pins_every_role_to_claude():
    arm = _arm("sonnet")
    roles = _role_config("sonnet")
    assert set(roles) == set(models._BENCH_ROLES)
    for role in models._BENCH_ROLES:
        assert roles[role] == _CLAUDE
    assert arm["mock"] is False
    assert arm["env"]["PIPELINE_BACKEND_DISPATCH"] == "claude"


def test_env_only_gptoss_claude_review_arm_stays_env_only():
    # Survivor: the pre-existing env-only arm is the control that shows what
    # env vars alone actually route, so it must not grow role pins.
    arm = models.MODELS["gptoss_claude_review"]
    env = arm["env"]
    assert env["PIPELINE_LOCAL_MAX_STEPS"] == "60"
    assert env["PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS"] == "900"
    assert env["PIPELINE_BACKEND_REVIEW"] == "claude"
    assert "role_config" not in arm


def _models_table() -> list[str]:
    readme = Path(models.__file__).resolve().parent / "README.md"
    text = readme.read_text()
    match = re.search(r"^## Models\s*$", text, re.MULTILINE)
    assert match, "tests/benchmark/README.md must keep its '## Models' section"
    section = text[match.end():]
    nxt = re.search(r"^## ", section, re.MULTILINE)
    if nxt:
        section = section[: nxt.start()]
    table = [ln for ln in section.splitlines() if ln.lstrip().startswith("|")]
    assert len(table) >= 2, "the Models section must contain its table"
    return table


def _cells(row: str) -> list[str]:
    # Split on unescaped pipes so a Notes cell containing an escaped `\|`
    # (legal markdown) is not miscounted as an extra column.
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", row.strip().strip("|"))]


def _row_for(name: str) -> list[str]:
    for row in _models_table()[2:]:
        cells = _cells(row)
        if cells and cells[0].strip("`") == name:
            return cells
    raise AssertionError(f"the Models table has no row for {name!r}")


@pytest.mark.parametrize("arm_name", sorted(_NEW_ARMS))
def test_readme_models_table_documents_each_published_arm(arm_name):
    header = _cells(_models_table()[0])
    assert len(header) >= 3, "the Models table must keep its Name/Backend/Notes columns"
    cells = _row_for(arm_name)
    # The new row must fit the table's existing columns, whatever their number.
    assert len(cells) == len(header)
    assert cells[0].strip("`") == arm_name
    assert cells[1], "the Backend cell must say what runs the arm"
    notes = cells[2].lower()
    assert "claude" in notes, "the Notes cell must say Claude plays the other roles"
    assert any(hint in notes for hint in _ROLE_HINTS), (
        "the Notes cell must say which roles Claude plays"
    )