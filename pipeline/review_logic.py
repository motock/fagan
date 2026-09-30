"""Fingerprint of the reviewing logic that forms a story's verdict.

A story the review gate rejected could never be re-reviewed unless a new commit
landed on its branch - even after the gate's own bug had since been fixed and
merged, because ``review_story`` skipped the reviewer whenever the worktree's
HEAD still equalled the SHA recorded at the last REQUEST_CHANGES. The skip must
additionally require that the reviewing logic which produced the last verdict is
the logic now installed.

This is a source digest rather than a hand-bumped constant because a constant
has to be remembered by hand on every logic change and is exactly the kind of
bookkeeping that gets forgotten - the digest cannot drift from the sources it
describes, so a fixed gate is picked up with no human step.

Deliberately excluded:

* the reviewer's *model* - that is configuration, not source; a story rejected
  by too weak a model is escalated through the rework path instead of being
  silently re-reviewed here.
* ``pipeline/server.py`` - it changes with nearly every story, so including it
  would re-review every stored verdict on every merge.

The modules are named as strings and read from disk rather than imported:
``review_orchestrator`` imports this module, so a real import would be circular.
"""

import hashlib
from pathlib import Path

REVIEW_LOGIC_FINGERPRINT_KEY = "last_reviewed_logic_fingerprint"

_VERDICT_FORMING_MODULES = (
    "scope_gate.py",
    "review.py",
    "review_orchestrator.py",
    "testfiles.py",
)


def review_logic_fingerprint(package_dir: Path | None = None) -> str:
    """Return a short SHA-256 digest of the verdict-forming modules' bytes.

    Each module's bytes are tagged with the module's filename and its byte
    length before the digest is updated, so bytes cannot be shifted across an
    entry boundary to forge a digest: ``scope_gate.py="ab"`` + ``review.py="c"``
    feeds ``"scope_gate.py2ab"`` and ``"review.py1c"``, while the one-byte move
    ``scope_gate.py="a"`` + ``review.py="bc"`` feeds ``"scope_gate.py1a"`` and
    ``"review.py2bc"`` - different digests.

    ``package_dir`` defaults to this package's directory and exists so a test
    can point the digest at a temp directory instead of monkeypatching
    internals.
    """
    if package_dir is None:
        package_dir = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in _VERDICT_FORMING_MODULES:
        module_bytes = (package_dir / name).read_bytes()
        digest.update(name.encode("utf-8"))
        digest.update(str(len(module_bytes)).encode("utf-8"))
        digest.update(module_bytes)
    return digest.hexdigest()[:16]


def is_unchanged_since_review(story: dict, current_sha: str) -> bool:
    """True only when both the HEAD SHA and the reviewing logic are unchanged.

    Both must hold: the story's ``last_reviewed_sha`` equals ``current_sha`` and
    the fingerprint stored under ``REVIEW_LOGIC_FINGERPRINT_KEY`` equals the
    digest of the reviewing logic now installed. A story recorded before this
    fingerprint existed stores none, so it is re-reviewed exactly once and then
    records the current digest.

    If the modules cannot be read the answer is False - fail toward re-review,
    never crash the review path.
    """
    if story.get("last_reviewed_sha") != current_sha:
        return False
    try:
        current_fingerprint = review_logic_fingerprint()
    except OSError:
        return False
    return story.get(REVIEW_LOGIC_FINGERPRINT_KEY) == current_fingerprint
