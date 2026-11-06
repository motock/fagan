"""Apply the story-sizing caps according to the dispatch target's strength class.

``pipeline.ingest._story_sizing_warning`` applies both caps from
`.claude/rules/agent-dispatch-story-sizing.md` to every non-Claude backend:
the 2-production-file cap and the 1000-line file-size cap. The file COUNT cap
is a crutch for small on-device models with a limited context/hold-in-head
budget; a capable dispatch target (Claude-class provider, or an ``ollama``
``:cloud`` model tag) does not need it. The file-SIZE cap still applies to
every class -- a 1200-line file is a problem for any model.

This module is the thin, non-raising adapter between the two:
``class_aware_sizing_warning`` returns the existing warning UNCHANGED for
on-device and unknown targets (so today's behavior is byte-identical), and
only filters the file-count reason out of a *capable* target's warning.
"""
from __future__ import annotations

from collections.abc import Callable

from .strength_class import CAPABLE, dispatch_strength_class

_PREFIX = "story sizing risk (see .claude/rules/agent-dispatch-story-sizing.md): "
_PRODUCTION_FILE_REASON = "production files ("
_WEAKEST_SUFFIX = " for the weakest dispatch tier"


def class_aware_sizing_warning(
    story: dict,
    repo_root: str,
    role_config: dict | None,
    on_device_warning: Callable[[dict, str], str | None],
) -> str | None:
    """Return the sizing warning appropriate to the story's dispatch class.

    *on_device_warning* is the existing ``_story_sizing_warning`` hook, called
    exactly once and returned unchanged unless the story dispatches to a
    capable model: only then is the 2-production-file cap lifted (its reason
    dropped) and the ``for the weakest dispatch tier`` qualifier removed from
    the file-size reason. Returns None when nothing remains to warn about.

    Note that the underlying hook gates on ``story["backend"]``, so a story
    that inherits the process-wide tier yields None there and this wrapper
    faithfully returns that None.
    """
    warning = on_device_warning(story, repo_root)
    if warning is None:
        return None
    try:
        if dispatch_strength_class(story, role_config) != CAPABLE:
            return warning
    except Exception:  # noqa: BLE001 - observability hook, never a gate
        return warning
    if not warning.startswith(_PREFIX):
        # An unexpected message shape: leave it alone rather than mangling it.
        return warning
    reasons = []
    for reason in warning[len(_PREFIX):].split("; "):
        if _PRODUCTION_FILE_REASON in reason:
            continue
        reason = reason.removesuffix(_WEAKEST_SUFFIX)
        reasons.append(reason)
    if not reasons:
        return None
    return _PREFIX + "; ".join(reasons)
