"""Acceptance oracle: overlord-policy.md must publish the parked-story
resolution content inside its "## Failure triage" section.

`pipeline.overlord._load_policy` ships this file verbatim as the overlord's
system prompt, so the shipped artifact is what is graded here - not a helper.
The story adds, additively, to the existing Failure triage section:

  (a) the parked-story decision matrix (evidence signal -> action),
  (b) foundation rule 1: never act on `parked_reason` text, re-derive live
      evidence (git state, suite state, PR state) before ruling,
  (c) foundation rule 2: historical park resolutions added human authority, not
      judgment; the overlord holds resolution authority and every executed
      action is reviewable post-hoc via the decisions log,
  (d) the autonomy-mode ladder (dry-run / gated / full),
  (e) the honest-terminal-park framing (some stories park permanently BY
      RULING - a correct outcome, not a failure).

MEMBERSHIP ONLY. overlord-policy.md is a cumulative shared artifact that later
sibling stories keep extending, so nothing here asserts the file's total
contents, exact counts, or byte equality of the file or of the section. Every
check is "this marker appears in the Failure triage section".

The ACTION contract line
"ACTION: escalate_model | split_story | repo_issue | park_for_human" is pinned
byte-for-byte by tests/unit/test_acceptance_triage_policy_contract.py (which
this story must keep green without editing), so it is deliberately not
re-pinned here.
"""

from __future__ import annotations

from pathlib import Path

_POLICY = Path(__file__).resolve().parents[2] / "overlord-policy.md"

_TRIAGE_HEADING = "## Failure triage"

# The four pre-existing ACTION values, and a distinctive phrase from each of
# their definitions. This story is additive: the definitions must survive
# un-removed and un-reworded.
_EXISTING_ACTIONS = ("escalate_model", "split_story", "repo_issue", "park_for_human")

_EXISTING_ACTION_DEFINITIONS = (
    "the scope is right, the implementer is too weak",
    "the scope is wrong for any implementer at this tier",
    "the failure is environmental, not the story's fault",
    "genuinely ambiguous; hold it",
)


def _policy_text() -> str:
    return _POLICY.read_text(encoding="utf-8") if _POLICY.is_file() else ""


def _triage_section() -> str:
    """The Failure triage section, up to the next top-level heading."""
    text = _policy_text()
    _, sep, rest = text.partition(_TRIAGE_HEADING)
    if not sep:
        return ""
    cut = rest.find("\n## ")
    if cut != -1:
        rest = rest[:cut]
    return rest


def _lower() -> str:
    return _triage_section().lower()


def _assert_any(text: str, options, msg: str) -> None:
    assert any(option in text for option in options), (
        f"{msg} (looked for any of {list(options)!r})"
    )


def _assert_all(text: str, needles, msg: str) -> None:
    missing = [needle for needle in needles if needle not in text]
    assert missing == [], f"{msg} (missing {missing!r})"


def _has_markdown_table(section: str) -> bool:
    rows = [line.strip() for line in section.splitlines() if line.strip().startswith("|")]
    if len(rows) < 2:
        return False
    return any(
        "|" in row and "-" in row and set(row) <= set("|-: ")
        for row in rows
    )


def _row_pairing(section: str, signal_options, action: str) -> bool:
    """True when some single line carries both a signal marker and the action."""
    for line in section.splitlines():
        low = line.lower()
        if action in low and any(option in low for option in signal_options):
            return True
    return False


# ---------------------------------------------------------------------------
# Pre-existing content that this additive story must not disturb.
# ---------------------------------------------------------------------------


def test_policy_file_is_present():
    assert _POLICY.is_file()


def test_failure_triage_section_is_present():
    assert _triage_section().strip() != ""


def test_output_contract_section_is_retained():
    assert "## Output contract" in _policy_text()


def test_existing_action_values_are_still_named_in_the_section():
    section = _lower()
    missing = [name for name in _EXISTING_ACTIONS if name not in section]
    assert missing == []


def test_existing_action_definitions_are_not_removed_or_reworded():
    section = _lower()
    missing = [phrase for phrase in _EXISTING_ACTION_DEFINITIONS if phrase not in section]
    assert missing == []


def test_fail_closed_sentence_is_retained():
    section = _lower()
    _assert_all(
        section,
        ("fail closed", "unparseable", "park_for_human"),
        "the Failure triage section must retain the fail-closed sentence",
    )


def test_park_and_ping_tier_override_rule_is_retained():
    section = _lower()
    _assert_any(
        section,
        ("never overrides the park", "never overrides the park-and-ping"),
        "the Failure triage section must retain the rule that triage never "
        "overrides the park-and-ping tier",
    )
    _assert_any(
        section,
        ("stays held regardless of the ruling", "stays held"),
        "the Failure triage section must retain the rule that a risk: high "
        "story stays held regardless of the ruling",
    )


# ---------------------------------------------------------------------------
# (a) The parked-story decision matrix.
# ---------------------------------------------------------------------------


def test_parked_story_decision_matrix_is_a_markdown_table():
    assert _has_markdown_table(_triage_section()), (
        "the parked-story decision matrix must be published as a compact "
        "markdown table inside the Failure triage section"
    )


def test_matrix_maps_stale_bookkeeping_to_mark_done():
    section = _triage_section()
    assert _row_pairing(section, ("stale bookkeeping",), "mark_done"), (
        "the matrix must map stale bookkeeping to mark_done"
    )


def test_matrix_names_the_stale_bookkeeping_evidence_signals():
    low = _lower()
    _assert_any(
        low,
        (
            "live state contradicts",
            "contradicts the recorded reason",
            "contradicts the recorded",
        ),
        "stale bookkeeping must be defined as live state contradicting the "
        "recorded reason",
    )
    _assert_any(
        low,
        ("suite green at head", "green suite at head", "suite is green at head"),
        "stale bookkeeping must include the 'suite green at HEAD' signal",
    )
    _assert_any(
        low,
        ("new commits", "commits vs base", "commits against base", "commits ahead of base"),
        "stale bookkeeping must include the 'branch has new commits vs base' signal",
    )
    _assert_any(
        low,
        ("pr merged", "pull request merged", "pr is merged", "merged pr"),
        "stale bookkeeping must include the 'PR merged' signal",
    )


def test_matrix_maps_rework_exhaustion_to_escalate_model():
    section = _triage_section()
    assert _row_pairing(
        section,
        ("rework exhaustion", "rework-exhaustion", "rework exhausted"),
        "escalate_model",
    ), "the matrix must map rework exhaustion to escalate_model"
    _assert_any(
        _lower(),
        ("mechanical leftovers", "mechanical leftover"),
        "the rework-exhaustion row must be qualified by mechanical leftovers",
    )


def test_matrix_maps_repeated_step_caps_to_split_story():
    section = _triage_section()
    assert _row_pairing(
        section,
        ("step-cap", "step cap", "step-caps", "step caps"),
        "split_story",
    ), "the matrix must map repeated step-caps to split_story"
    _assert_any(
        _lower(),
        ("oversized scope", "oversized"),
        "the repeated-step-caps row must be qualified by oversized scope",
    )


def test_matrix_maps_broken_acceptance_fixture_to_patch_acceptance():
    section = _triage_section()
    assert _row_pairing(
        section,
        ("acceptance fixture", "acceptance oracle"),
        "patch_acceptance",
    ), "the matrix must map a broken acceptance fixture to patch_acceptance"
    _assert_any(
        _lower(),
        ("clean baseline", "clean-baseline"),
        "the broken-acceptance-fixture row must be qualified by a clean baseline",
    )
    _assert_any(
        _lower(),
        ("demonstrably broken", "demonstrably", "broken at a clean baseline"),
        "the broken-acceptance-fixture row must say the fixture is demonstrably broken",
    )


def test_matrix_holds_risk_high_merges_in_dry_run_and_gated():
    section = _triage_section()
    low = section.lower()
    _assert_any(low, ("risk: high", "risk:high", "risk high"), "the matrix must name risk: high")
    assert _row_pairing(section, ("risk: high", "risk:high", "risk high"), "held"), (
        "the risk: high merge-hold row must say the merge is held"
    )
    _assert_any(low, ("dry-run", "dry run"), "the risk: high row must mention dry-run")
    _assert_any(low, ("gated",), "the risk: high row must mention gated")
    _assert_any(
        low,
        ("adjudicat",),
        "the risk: high row must say the overlord adjudicates it in full",
    )


def test_matrix_keeps_abandoned_or_superseded_scope_parked_by_ruling():
    section = _triage_section()
    low = section.lower()
    _assert_any(low, ("abandoned",), "the matrix must name abandoned scope")
    _assert_any(low, ("superseded",), "the matrix must name superseded scope")
    assert _row_pairing(section, ("abandoned", "superseded"), "park"), (
        "the abandoned/superseded row must say the story stays parked"
    )
    _assert_any(low, ("by ruling",), "the abandoned/superseded row must say BY RULING")
    _assert_any(
        low,
        ("recorded reasoning", "reasoning is recorded", "recorded rationale"),
        "the abandoned/superseded row must require recorded reasoning",
    )


# ---------------------------------------------------------------------------
# (b) Foundation rule 1: re-derive live evidence, never trust parked_reason.
# ---------------------------------------------------------------------------


def test_foundation_rule_one_never_acts_on_parked_reason_text():
    low = _lower()
    assert "parked_reason" in low, (
        "foundation rule 1 must name the `parked_reason` text it forbids acting on"
    )
    _assert_any(
        low,
        ("never act on", "never acts on", "do not act on", "never trust"),
        "foundation rule 1 must forbid acting on parked_reason text",
    )


def test_foundation_rule_one_requires_re_deriving_live_evidence():
    low = _lower()
    _assert_any(
        low,
        ("re-derive", "re-derives", "rederive", "re-derive live evidence"),
        "foundation rule 1 must require re-deriving live evidence before ruling",
    )
    _assert_any(
        low,
        ("live evidence", "live state"),
        "foundation rule 1 must speak of live evidence",
    )
    _assert_any(low, ("git state", "git status"), "live evidence must include git state")
    _assert_any(low, ("suite state", "suite status"), "live evidence must include suite state")
    _assert_any(
        low,
        ("pr state", "pr status", "pull request state"),
        "live evidence must include PR state",
    )


# ---------------------------------------------------------------------------
# (c) Foundation rule 2: authority, not judgment; decisions log.
# ---------------------------------------------------------------------------


def test_foundation_rule_two_says_history_added_human_authority_not_judgment():
    low = _lower()
    _assert_any(
        low,
        ("human authority", "authority, not judgment", "authority, not judgement"),
        "foundation rule 2 must say historical park resolutions added human authority",
    )
    _assert_any(
        low,
        ("not judgment", "not judgement"),
        "foundation rule 2 must say the history added authority, not judgment",
    )


def test_foundation_rule_two_gives_the_overlord_resolution_authority():
    low = _lower()
    _assert_any(
        low,
        ("resolution authority", "holds resolution authority"),
        "foundation rule 2 must state that the overlord holds resolution authority",
    )


def test_foundation_rule_two_makes_executed_actions_reviewable_via_the_decisions_log():
    low = _lower()
    _assert_any(low, ("decisions log", "decision log"), "the decisions log must be named")
    _assert_any(low, ("reviewable",), "executed actions must be reviewable")
    _assert_any(
        low,
        ("post-hoc", "post hoc", "after the fact"),
        "reviewability must be post-hoc",
    )


# ---------------------------------------------------------------------------
# (d) The autonomy-mode ladder.
# ---------------------------------------------------------------------------


def test_autonomy_ladder_dry_run_is_notify_only():
    low = _lower()
    _assert_any(low, ("dry-run", "dry run"), "the ladder must name dry-run")
    _assert_any(
        low,
        ("notify only", "notify-only", "notification only"),
        "dry-run must be defined as notify only",
    )


def test_autonomy_ladder_gated_executes_reversible_manifest_only_actions():
    section = _triage_section()
    low = section.lower()
    _assert_any(low, ("gated",), "the ladder must name gated")
    _assert_any(low, ("reversible",), "gated actions must be reversible")
    _assert_any(low, ("manifest-only", "manifest only"), "gated actions must be manifest-only")
    _assert_all(
        low,
        ("mark_done", "split_story", "patch_acceptance"),
        "the gated rung must name the reversible manifest-only actions",
    )


def test_autonomy_ladder_gated_still_holds_risk_high_merges():
    low = _lower()
    _assert_any(
        low,
        ("still held", "held", "stays held"),
        "the gated rung must keep risk: high merges held",
    )


def test_autonomy_ladder_full_adds_overlord_adjudication_of_risk_high_merges():
    low = _lower()
    _assert_any(
        low,
        (
            "gated plus",
            "gated +",
            "full = gated",
            "full: gated",
            "full adds",
            "full mode adds",
            "gated, plus",
        ),
        "the full rung must be defined as gated plus something",
    )
    _assert_any(
        low,
        ("adjudicat",),
        "the full rung must add overlord adjudication of risk: high merges",
    )


# ---------------------------------------------------------------------------
# (e) The honest-terminal-park framing.
# ---------------------------------------------------------------------------


def test_honest_terminal_park_is_a_correct_outcome_not_a_failure():
    low = _lower()
    _assert_any(
        low,
        ("permanently", "permanent"),
        "the framing must say some stories park permanently",
    )
    _assert_any(low, ("by ruling",), "the framing must say the park is BY RULING")
    _assert_any(
        low,
        ("not a failure", "is not a failure"),
        "the framing must say a terminal park is not a failure",
    )
    _assert_any(
        low,
        ("recorded reasoning", "reasoning is recorded", "recorded rationale"),
        "the framing must require recorded reasoning",
    )
    _assert_any(
        low,
        ("abandoned", "superseded"),
        "the framing must scope terminal parks to abandoned or superseded scope",
    )


# ---------------------------------------------------------------------------
# Placement: the new rulings belong to the Failure triage section itself.
# ---------------------------------------------------------------------------


def test_new_rulings_live_inside_the_failure_triage_section():
    section = _triage_section().lower()
    _assert_all(
        section,
        ("mark_done", "patch_acceptance", "parked_reason", "by ruling"),
        "the parked-story resolution content must be added to the Failure "
        "triage section, not somewhere else in the policy",
    )
