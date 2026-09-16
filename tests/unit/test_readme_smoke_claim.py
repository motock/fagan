"""Tests for the README's getting-started smoke claim (documentation correction).

The prerequisite story (PN-1) changed ``scripts/smoke_getting_started.py`` so it
runs on WHATEVER dispatch provider the operator has configured, announcing it up
front, instead of exiting 2 on anything but ``claude``. The README's
Getting-started walkthrough still framed the whole walkthrough - and step 7 of
it is the smoke - as claude-only:

    "No local model is required anywhere in this walkthrough: with
     `PIPELINE_BACKEND_DISPATCH=claude` ... dispatch and review shell out to
     the Claude Code CLI and never touch ollama."

That is now wrong. The corrected README must say that the smoke runs on the
operator's configured dispatch provider (and prints which one it validated),
that exit 2 means the configured provider is empty or unrecognised - a
configuration error, NOT a refusal of a local provider - and the honest caveat
that PASS depends on the configured model actually completing the story.

These tests read README.md and assert on its text, so they are RED until the
prose is corrected. They are self-contained: they do not import the smoke
script or any implementation module.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
README = REPO_ROOT / "README.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def h2_headings(text: str) -> list[str]:
    """Return the ordered list of H2 (##) heading titles in `text`.

    Same extraction approach as tests/unit/test_readme_reference_split.py:
    headings are matched on lines that start at column 0 with '## '.
    """
    headings: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            headings.append(line[3:].strip())
    return headings


# The exact ordered H2 list pinned by tests/unit/test_readme_reference_split.py.
# This story is a prose-only correction: it must not add or remove any '##'
# heading, so the count and order are unchanged.
EXPECTED_README_SECTIONS = [
    "Platform support",
    "Quickstart",
    "Components at a glance",
    "Architecture",
    "Personas (`~/.claude/agents/`)",
    "The overlord and the decision policy",
    "Reference",
    "Prerequisites",
    "Scheduler",
    "Reliability & limitations",
    "License",
]


def _readme_text() -> str:
    assert README.is_file(), "README.md must exist at the repo root"
    return README.read_text()


def smoke_region(text: str) -> str:
    """Return the README prose documenting the getting-started smoke.

    The region starts at the line naming ``scripts/smoke_getting_started.py``
    and runs to the next markdown heading, so it covers the smoke bullet plus
    any prose the correction adds around it.
    """
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if "smoke_getting_started" in line),
        None,
    )
    assert start is not None, (
        "README.md must document scripts/smoke_getting_started.py"
    )
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## ") or lines[j].startswith("### "):
            end = j
            break
    return "\n".join(lines[start:end])


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(needle.lower() in low for needle in needles)


# ---------------------------------------------------------------------------
# 1. No claim that the smoke requires / is limited to the claude backend
# ---------------------------------------------------------------------------

# Stale phrasing found in README.md before this correction (Getting-started
# walkthrough intro, ~lines 193-198), quoted verbatim so a future reader knows
# what was removed:
#
#     "No local model is required anywhere in this walkthrough: with
#      `PIPELINE_BACKEND_DISPATCH=claude` (set it explicitly, or add a `roles`
#      block to a local registry ...) dispatch and review shell out to the
#      Claude Code CLI and never touch ollama."
#
# The smoke is step 7 of that walkthrough, so this told the reader the smoke
# was claude-only and never touched a local backend. It is now wrong: the smoke
# runs on whatever provider the operator configured.
STALE_CLAUDE_ONLY_PHRASES = (
    "never touch ollama",
    "No local model is required anywhere in this walkthrough",
    "refuses to run on any local-family backend",
    "exit 2 by design",
    "Claude-backed install",
    "requires the `claude` backend",
    "requires the claude backend",
    "only the `claude` backend",
    "limited to the `claude` backend",
)


@pytest.mark.parametrize("phrase", STALE_CLAUDE_ONLY_PHRASES)
def test_readme_has_no_stale_claude_only_smoke_claim(phrase: str) -> None:
    text = _readme_text()
    assert phrase not in text, (
        "README.md must not claim the smoke requires or is limited to the "
        f"claude backend; stale phrase still present: {phrase!r}"
    )


def test_readme_smoke_region_does_not_claim_claude_only() -> None:
    region = smoke_region(_readme_text())
    low = region.lower()
    for phrase in (
        "never touch ollama",
        "requires the `claude` backend",
        "only the `claude` backend",
        "limited to the `claude` backend",
    ):
        assert phrase not in low, (
            "the README's smoke documentation must not claim the smoke is "
            f"claude-only; found {phrase!r} in:\n{region}"
        )


# ---------------------------------------------------------------------------
# 2. The smoke mention uses .venv/bin/python
# ---------------------------------------------------------------------------

def test_readme_smoke_uses_venv_python() -> None:
    region = smoke_region(_readme_text())
    assert ".venv/bin/python" in region, (
        "the README's smoke mention must use `.venv/bin/python` - the "
        "dependencies live in the virtualenv, so a bare `python` fails with "
        f"ModuleNotFoundError. Smoke region was:\n{region}"
    )


# ---------------------------------------------------------------------------
# 3. The corrected text conveys the post-change behavior
# ---------------------------------------------------------------------------

def test_readme_smoke_runs_on_the_configured_dispatch_provider() -> None:
    region = smoke_region(_readme_text())
    low = region.lower()
    assert "configured" in low, (
        "the README must say the smoke runs on the operator's CONFIGURED "
        f"dispatch provider; smoke region was:\n{region}"
    )
    assert "provider" in low, (
        "the README must name the dispatch provider the smoke runs on; "
        f"smoke region was:\n{region}"
    )
    assert _contains_any(
        region, ("announce", "print", "name", "report", "tell")
    ), (
        "the README must say the smoke announces/prints which provider it "
        f"validated; smoke region was:\n{region}"
    )


def test_readme_smoke_exit_2_is_a_configuration_error() -> None:
    region = smoke_region(_readme_text())
    low = region.lower()
    assert _contains_any(region, ("exit 2", "exit code 2", "exits 2")), (
        "the README must document what exit 2 now means for the smoke; "
        f"smoke region was:\n{region}"
    )
    assert "empty" in low, (
        "the README must say exit 2 means the configured provider is EMPTY; "
        f"smoke region was:\n{region}"
    )
    assert _contains_any(region, ("unrecognis", "unrecogniz", "unknown")), (
        "the README must say exit 2 means the configured provider is "
        f"UNRECOGNISED; smoke region was:\n{region}"
    )
    assert _contains_any(
        region, ("configuration error", "config error", "misconfigur")
    ), (
        "the README must call exit 2 a configuration error, not a refusal of "
        f"a local provider; smoke region was:\n{region}"
    )


def test_readme_smoke_pass_caveat_names_the_configured_model() -> None:
    region = smoke_region(_readme_text())
    low = region.lower()
    assert "pass" in low, (
        "the README must keep the smoke's PASS success bar in view; "
        f"smoke region was:\n{region}"
    )
    assert "model" in low, (
        "the README must carry the honest caveat that PASS depends on the "
        f"configured MODEL completing the story; smoke region was:\n{region}"
    )
    assert _contains_any(
        region,
        (
            "broken pipeline",
            "not the pipeline",
            "not a pipeline",
            "reflect",
            "model you configured",
            "configured model",
        ),
    ), (
        "the README must say a failure on a weak local model reflects that "
        f"model, not a broken pipeline; smoke region was:\n{region}"
    )


# ---------------------------------------------------------------------------
# 4. NEGATIVE / BOUNDARY: the H2 headings are unchanged in count and order
# ---------------------------------------------------------------------------

def test_readme_h2_headings_unchanged_in_count_and_order() -> None:
    headings = h2_headings(_readme_text())
    assert headings == EXPECTED_README_SECTIONS, (
        "README.md H2 sections must be unchanged (this is a prose-only "
        f"correction); expected {EXPECTED_README_SECTIONS}; got {headings}"
    )


def test_readme_has_eleven_h2_sections() -> None:
    headings = h2_headings(_readme_text())
    assert len(headings) == 11, (
        "README.md must keep exactly 11 H2 sections, got "
        f"{len(headings)}: {headings}"
    )
