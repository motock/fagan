"""READMEPITCH-1: README must open with the cost-tiered thesis.

The README's preamble (everything before the first ``## `` heading) must pitch
the pipeline's cost split — frontier models for judgment, local models for
implementation — instead of describing the project as a quickstart guide.
Per-story test file so sibling stories never collide on a shared assertion.
"""

from pathlib import Path

README_PATH = Path(__file__).resolve().parents[2] / "README.md"


def _readme_text() -> str:
    return README_PATH.read_text(encoding="utf-8")


def _readme_preamble() -> str:
    """Every line of README.md before the first line starting with '## '."""
    lines = _readme_text().splitlines()
    preamble_lines = []
    for line in lines:
        if line.startswith("## "):
            break
        preamble_lines.append(line)
    return "\n".join(preamble_lines)


def test_preamble_leads_with_token_thesis_line():
    preamble = _readme_preamble()
    assert "Spend tokens on judgment, not typing." in preamble


def test_preamble_names_both_sides_of_cost_split():
    preamble = _readme_preamble()
    assert "Frontier models" in preamble
    assert "Local models" in preamble


def test_preamble_states_budget_claim():
    preamble = _readme_preamble()
    assert "$20/month" in preamble


def test_preamble_credits_fagan_inspection_rationale():
    preamble = _readme_preamble()
    assert "Fagan" in preamble
    assert "82%" in preamble


def test_readme_no_longer_calls_project_a_quickstart_guide():
    text = _readme_text()
    assert (
        "This is a quickstart guide for the Autonomous SDLC Agent Pipeline"
        not in text
    )


def test_quickstart_claude_default_framed_as_starting_point():
    text = _readme_text()
    assert "the *starting* configuration, not the intended one" in text