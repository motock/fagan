"""One notification per triage sweep, describing what the overlord did.

``run_triage_sweep`` hands :func:`sweep_digest` one row per ruling it acted
on. The digest names park classes, actions and story keys, and counts each
outcome as ``executed``, ``shadow`` or ``held``. It never carries a ruling's
rationale or any file content.
"""

from collections import Counter

MAX_DIGEST_CHARS = 1000

# Outcomes of ``_apply_ruling_for_mode`` that did not act on the story.
_HELD_OUTCOMES = frozenset({"dry-run", "park_for_human"})
_GROUPS = ("Executed", "Shadow", "Held")


def _group(outcome: str) -> str:
    if outcome == "shadow":
        return "Shadow"
    return "Held" if outcome in _HELD_OUTCOMES else "Executed"


def _counts(counter: Counter) -> str:
    return ", ".join(f"{name} {n}" for name, n in sorted(counter.items()))


def sweep_digest(results: list[dict]) -> str | None:
    """One line describing a sweep, or ``None`` when it ruled on nothing.

    Each row carries ``story_key``, ``park_class``, ``action`` and ``outcome``.
    """
    if not results:
        return None
    keys = {group: [] for group in _GROUPS}
    for row in results:
        keys[_group(row["outcome"])].append(row["story_key"])
    parts = [
        f"Overlord triage sweep: {len(results)} ruling(s).",
        f"executed {len(keys['Executed'])}, shadow {len(keys['Shadow'])}, held {len(keys['Held'])}.",
        f"Park class: {_counts(Counter(row['park_class'] for row in results))}.",
        f"Action: {_counts(Counter(row['action'] for row in results))}.",
    ]
    parts += [f"{group}: {', '.join(keys[group])}." for group in _GROUPS if keys[group]]
    text = " ".join(parts)
    if len(text) <= MAX_DIGEST_CHARS:
        return text
    return text[: MAX_DIGEST_CHARS - 3] + "..."
