"""The decompose prompt tells the analyst to keep a change with the tests that pin it.

A story's pull request is gated by the repository's own CI, so the decomposer
must never split "change the value" from "update the test that asserts it".
These tests pin the guidance in the system prompt for every target tier, that
it sits alongside the tier's own guidance, that it is appended once, and that
the rule file the analyst is pointed at carries the same rule without any
personal paths.
"""

from pathlib import Path

import pytest

from pipeline import persona as pper
from pipeline import planner

RULE_FILE = Path(__file__).resolve().parents[2] / ".claude" / "rules" / "agent-dispatch-story-sizing.md"
GUIDANCE_SENTENCE = "never put the change in one story and the test update in a later one"

# A dispatch route per tier: the tier is classified from the resolved dispatch model.
TIER_ROUTING = {
    "claude": {"provider": "claude", "model": "opus"},
    "cloud-oss": {"provider": "ollama", "model": "glm-5.3-flash:cloud"},
    "local": {"provider": "ollama", "model": "devstral"},
}
TIERS = list(TIER_ROUTING)


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "product-analyst.md").write_text(
        '---\nname: "product-analyst"\nmodel: opus\n---\n\nAnalyst body.\n'
    )
    monkeypatch.setattr(pper, "AGENTS_DIR", d)
    return d


def _install_backend(monkeypatch):
    """Record every complete() call and return a valid empty plan."""
    from app import backend

    calls = []

    class _Driver:
        def complete(self, prompt, *, system, model, allowed_tools):
            calls.append({"system": system})
            return '{"epics": []}'

    monkeypatch.setattr(backend, "get_backend", lambda role, *, name: _Driver())
    return calls


def _decompose_system_prompt(monkeypatch, agents_dir, tier):
    calls = _install_backend(monkeypatch)
    text, error = planner._run_decompose_detailed(
        "goal",
        layer_routing={"decompose": {"provider": "ollama", "model": "gpt-oss"}, "dispatch": TIER_ROUTING[tier]},
    )
    assert error is None
    assert text
    assert len(calls) == 1
    return calls[0]["system"]


@pytest.mark.parametrize("tier", TIERS)
def test_decompose_prompt_keeps_a_change_with_its_tests(monkeypatch, agents_dir, tier):
    system = _decompose_system_prompt(monkeypatch, agents_dir, tier)

    assert GUIDANCE_SENTENCE in system


@pytest.mark.parametrize("tier", TIERS)
def test_green_on_its_own_guidance_keeps_the_tier_guidance(monkeypatch, agents_dir, tier):
    system = _decompose_system_prompt(monkeypatch, agents_dir, tier)

    assert planner._STRENGTH_TIER_GUIDANCE[tier] in system


@pytest.mark.parametrize("tier", TIERS)
def test_green_on_its_own_guidance_appears_once(monkeypatch, agents_dir, tier):
    system = _decompose_system_prompt(monkeypatch, agents_dir, tier)

    assert system.count(GUIDANCE_SENTENCE) == 1


def test_sizing_rule_file_has_the_green_on_its_own_heading():
    assert "A story must be green on its own" in RULE_FILE.read_text(encoding="utf-8")


def test_sizing_rule_file_has_no_personal_paths():
    text = RULE_FILE.read_text(encoding="utf-8")

    assert "/Users/" not in text
    assert "/home/" not in text
