"""The security reviewer is told the findings format the review gate parses (srf-01).

A REQUEST_CHANGES whose body is longer than UNFOCUSED_REQUEST_CHANGES_MIN_CHARS
and carries no `- Blocking: <path>:` or `- Blocking: (unscoped)` line is treated
as inconclusive. These tests pin the prompt contract, the persona example, and
that the contract and the parser agree.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import pipeline.review as review_mod
import pipeline.server  # noqa: F401
from pipeline import parsers

REPO_ROOT = Path(__file__).resolve().parents[2]
PERSONA = REPO_ROOT / "agents" / "security-engineer.md"

FILE_LINE = "- Blocking: <relative/file/path>: <one-line description>"
UNSCOPED_LINE = "- Blocking: (unscoped)"
VERDICT_SENTENCE = "End with your VERDICT line: APPROVE or REQUEST_CHANGES."
EXAMPLE_FILE = "overlay/jobs/spec.py"

# The measured shape from the 19 rejected security reviews: heading, bold
# numbered finding, backticked path in parentheses, then the verdict.
MARKDOWN_BODY = (
    "### Blocking\n\n"
    "**1. The runner-side env filter is a denylist** "
    f"(`{EXAMPLE_FILE}`, `_DENIED_PREFIXES`)\n\n"
    "The filter drops only the prefixes it knows about, so any new secret "
    "variable name passes through to the job runner. An attacker who can set "
    "an environment variable on the host reaches the runner. Fix: switch to an "
    "allowlist of the variables the runner needs and deny everything else.\n\n"
    "VERDICT: REQUEST_CHANGES\n"
)


# Pads a body past UNFOCUSED_REQUEST_CHANGES_MIN_CHARS so the length gate applies.
PADDING = "\n\n" + " The reasoning continues across this sentence to add detail." * 20


def _capture_prompt(monkeypatch, **kwargs) -> str:
    captured = {}

    class FakeBackend:
        def complete(self, prompt, **kw):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(review_mod.backend, "get_backend", lambda *a, **k: FakeBackend())
    monkeypatch.setattr(review_mod, "_persona_body", lambda name: "persona")
    review_mod._run_security_reviewer("/tmp/wt", "agent/SRF-01", **kwargs)
    return captured["prompt"]


@pytest.fixture
def first_prompt(monkeypatch) -> str:
    return _capture_prompt(monkeypatch)


@pytest.fixture
def rework_prompt(monkeypatch) -> str:
    return _capture_prompt(monkeypatch, since_sha="abc1234")


def test_first_review_prompt_states_file_scoped_findings_form(first_prompt):
    assert FILE_LINE in first_prompt


def test_first_review_prompt_states_unscoped_findings_form(first_prompt):
    assert UNSCOPED_LINE in first_prompt


def test_first_review_prompt_keeps_verdict_sentence_at_end(first_prompt):
    assert first_prompt.endswith(VERDICT_SENTENCE)


def test_first_review_prompt_states_contract_before_verdict_sentence(first_prompt):
    assert first_prompt.index(review_mod.SECURITY_FINDINGS_CONTRACT) < first_prompt.index(VERDICT_SENTENCE)


def test_rework_review_prompt_states_file_scoped_findings_form(rework_prompt):
    assert FILE_LINE in rework_prompt


def test_rework_review_prompt_states_unscoped_findings_form(rework_prompt):
    assert UNSCOPED_LINE in rework_prompt


def test_rework_review_prompt_keeps_verdict_sentence_at_end(rework_prompt):
    assert rework_prompt.endswith(VERDICT_SENTENCE)


def test_context_block_follows_verdict_sentence(monkeypatch):
    prompt = _capture_prompt(monkeypatch, context_block="CTX-BLOCK")
    assert prompt.index(VERDICT_SENTENCE) < prompt.index("CTX-BLOCK")


def test_file_scoped_form_from_contract_is_parseable_and_yields_path():
    body = (
        f"- Blocking: {EXAMPLE_FILE}: the env filter is a denylist, so new secret names leak.\n"
        "VERDICT: REQUEST_CHANGES" + PADDING
    )
    assert len(body) > parsers.UNFOCUSED_REQUEST_CHANGES_MIN_CHARS
    assert parsers.findings_are_parseable(body)
    assert parsers._extract_blocking_finding_files(body) == [EXAMPLE_FILE]


def test_unscoped_form_from_contract_is_parseable():
    body = (
        "- Blocking: (unscoped) the review gate accepts a verdict with no findings.\n"
        "VERDICT: REQUEST_CHANGES" + PADDING
    )
    assert len(body) > parsers.UNFOCUSED_REQUEST_CHANGES_MIN_CHARS
    assert parsers.findings_are_parseable(body)


def test_measured_markdown_shape_is_not_parseable():
    body = MARKDOWN_BODY + PADDING
    assert len(body) > parsers.UNFOCUSED_REQUEST_CHANGES_MIN_CHARS
    assert not parsers.findings_are_parseable(body)


def test_backticked_path_in_file_scoped_line_yields_path_with_backticks():
    line = f"- Blocking: `{EXAMPLE_FILE}`: the env filter is a denylist."
    assert parsers._extract_blocking_finding_files(line) == [f"`{EXAMPLE_FILE}`"]


def test_persona_shows_file_scoped_example_line():
    assert f"- Blocking: {EXAMPLE_FILE}: " in PERSONA.read_text(encoding="utf-8")


def test_persona_shows_unscoped_example_line():
    assert "- Blocking: (unscoped) " in PERSONA.read_text(encoding="utf-8")
