"""Docs contract: the policy must not claim `full` always holds high-risk merges.

OPSA-9 made ``PIPELINE_AUTONOMY=full`` adjudicate a ``risk: high`` merge instead
of holding it unconditionally. The prose in ``overlord-policy.md`` and
``REFERENCE.md`` is what the overlord reads as its instructions, so a stale
"always held regardless of level" sentence there silently re-teaches the old
behaviour even though the code has moved on.

Four guards, each of which fails if the old sentence is restored:

1. neither doc claims a hold that is unconditional (``which is always held``);
2. the policy names the authority matrix it is subordinate to
   (``overlord-authority.json``, ADR 0014);
3. both the policy's and README's ``full`` description say the overlord
   *adjudicates* the high-risk merge rather than holding it;
4. the policy reconciles with the matrix actually shipped in this repo: the
   shipped matrix resolves ``high_risk_merge`` to ``human``
   (``authority_matrix.tier_for``), so the paragraph that cites it may not also
   promise that every ``human`` class parks while ``full`` adjudicates one of
   them. The paragraph must name the unbound merge gate as the reason the
   ``full`` bullet is the rule in force today.

The raw documents are hard-wrapped, so a naive substring search cannot see a
sentence split across a line break. Everything here runs through
``_policy_flat``, which hyphen-normalises and collapses all whitespace first -
the same helper body as ``tests/unit/test_merge_overlord_adjudication.py``.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = REPO_ROOT / "overlord-policy.md"
REFERENCE_PATH = REPO_ROOT / "REFERENCE.md"
README_PATH = REPO_ROOT / "README.md"


def _policy_flat(path):
    text = Path(path).read_text(encoding="utf-8")
    text = text.replace("\u2011", "-").replace("\u2010", "-")
    return re.sub(r"\s+", " ", text)


def test_no_always_held_claim():
    for path in (POLICY_PATH, REFERENCE_PATH):
        flat = _policy_flat(path)
        assert "which is always held" not in flat, (
            f"{path.name} still claims the park-and-ping tier is held "
            "unconditionally; in `full` the overlord adjudicates it instead"
        )


def test_policy_cites_authority_matrix():
    flat = _policy_flat(POLICY_PATH)
    assert "overlord-authority.json" in flat, (
        "overlord-policy.md must point at overlord-authority.json (ADR 0014) "
        "for the classes of decision it may take at each tier"
    )


def test_full_level_is_adjudicated():
    policy_flat = _policy_flat(POLICY_PATH)
    policy_start = policy_flat.index("- `full`")
    # The flattened doc has no newlines, so the bullet ends at the next bullet
    # marker (" - ") or the next heading (" ## ") - whichever comes first.
    # Bounding on "- `" alone runs past the end of the Autonomy levels section.
    policy_end = min(
        idx
        for idx in (
            policy_flat.find(" - ", policy_start + 1),
            policy_flat.find(" ## ", policy_start + 1),
        )
        if idx != -1
    )
    policy_bullet = policy_flat[policy_start:policy_end]
    print(f"policy full bullet: {policy_bullet!r}")
    assert "adjudicat" in policy_bullet.lower(), (
        "the policy's `full` bullet must say the overlord adjudicates the "
        f"high-risk merge; got: {policy_bullet!r}"
    )

    # README has no `- \`full\`` bullet: its description of `full` is the
    # park-and-ping bullet in "The overlord and the decision policy" section.
    # Window on that bullet rather than on a bullet marker that is not there.
    readme_flat = _policy_flat(README_PATH)
    readme_start = readme_flat.index("**Park-and-ping**")
    readme_window = readme_flat[readme_start : readme_start + 600]
    print(f"README park-and-ping bullet: {readme_window!r}")
    assert "`full`" in readme_window, (
        "README's park-and-ping bullet should describe what `full` does; "
        f"got: {readme_window!r}"
    )
    assert "adjudicat" in readme_window.lower(), (
        "README's description of `full` must say the overlord adjudicates the "
        f"high-risk merge; got: {readme_window!r}"
    )


def test_policy_reconciles_with_shipped_matrix():
    """One rule must stand: the matrix's `human` tier and the `full` bullet agree.

    ``overlord-authority.json`` pins ``high_risk_merge`` to ``human`` and
    ``authority_matrix.tier_for`` folds the rule's ``value``, so the matrix
    shipped in this repo answers ``human`` for a high-risk merge. The policy
    therefore cannot cite the matrix *and* claim that every ``human`` class
    parks while ``full`` adjudicates one of them. What it must say instead is
    that the merge gate does not read the matrix yet - so the ``full`` bullet
    is the rule in force - and that binding the gate would park the merge at
    every level.
    """
    from pipeline.authority_matrix import FileLayerSource, tier_for

    resolved = FileLayerSource(REPO_ROOT / "overlord-authority.json").resolve()
    assert resolved is not None, "the shipped overlord-authority.json must load"
    assert tier_for(resolved, "high_risk_merge") == "human", (
        "the shipped matrix resolves high_risk_merge to `human`; if that "
        "changes, the policy paragraph and this test must change with it"
    )

    flat = _policy_flat(POLICY_PATH)
    start = flat.index("The classes of decision")
    end = flat.index("## Autonomy levels", start)
    paragraph = flat[start:end]
    print(f"merge adjudication matrix paragraph: {paragraph!r}")

    assert "The merge gate is not yet bound to the matrix" in paragraph, (
        "the paragraph must name the unbound merge gate as the reason `full` "
        f"still adjudicates; got: {paragraph!r}"
    )
    assert "resolves to `human` and the merge parks at every level" in paragraph, (
        "the paragraph must state that folding the matrix parks the high-risk "
        f"merge at every level; got: {paragraph!r}"
    )
    # Fails if the contradictory form is restored: the matrix's `human` tier
    # applied to every class while the `full` bullet adjudicates one of them.
    assert (
        "Where that file says `human`, the overlord parks regardless of the "
        "autonomy level." not in flat
    ), (
        "the blanket `human` sentence is back; it contradicts the `full` "
        "adjudication bullet because the shipped matrix says `human` for "
        "high_risk_merge"
    )
