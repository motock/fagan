"""Every third-party or first-party action in a workflow is pinned to a commit SHA.

A tag such as ``actions/checkout@v7.0.1`` is a mutable ref: its target can move
and change what CI runs. Each non-local ``uses:`` must end in ``@`` plus exactly
40 lowercase hex characters.

PyYAML is not a dependency of this repo, so workflows are read as text.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

USES = re.compile(r"^\s*(?:- )?uses:\s*(\S+)\s*$")
PINNED_SHA = re.compile(r"@[0-9a-f]{40}$")


def _is_pinned(ref: str) -> bool:
    if ref.startswith("./"):
        return True
    return PINNED_SHA.search(ref) is not None


def _uses_refs(text: str) -> list[str]:
    refs = []
    for line in text.splitlines():
        code = re.sub(r"\s+#.*$", "", line)
        match = USES.match(code)
        if match:
            refs.append(match.group(1))
    return refs


def test_every_workflow_action_is_pinned_to_a_commit_sha() -> None:
    workflows = sorted(WORKFLOWS.glob("*.yml"))
    assert workflows, f"no workflows found under {WORKFLOWS}"

    unpinned = [
        f"{path.name}: {ref}"
        for path in workflows
        for ref in _uses_refs(path.read_text(encoding="utf-8"))
        if not _is_pinned(ref)
    ]

    assert unpinned == []


def test_checker_rejects_tag_branch_and_short_sha_refs() -> None:
    assert not _is_pinned("actions/checkout@v7")
    assert not _is_pinned("actions/checkout@main")
    assert not _is_pinned("actions/checkout@" + "a" * 39)
