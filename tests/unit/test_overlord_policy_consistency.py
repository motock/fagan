"""Docs contract: the policy must not claim `full` always holds high-risk merges.

OPSA-9 made ``PIPELINE_AUTONOMY=full`` adjudicate a ``risk: high`` merge instead
of holding it unconditionally. The prose in ``overlord-policy.md`` and
``REFERENCE.md`` is what the overlord reads as its instructions, so a stale
"always held regardless of level" sentence there silently re-teaches the old
behaviour even though the code has moved on.

Three guards, each of which fails if the old sentence is restored:

1. neither doc claims a hold that is unconditional (``which is always held``);
2. the policy names the authority matrix it is subordinate to
   (``overlord-authority.json``, ADR 0014);
3. both the policy's and README's ``full`` description say the overlord
   *adjudicates* the high-risk merge rather than holding it.

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
