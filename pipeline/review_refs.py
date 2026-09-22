"""Lazily-resolved references to attributes of ``pipeline.server``.

WHY this indirection exists: the review-orchestration body in
``pipeline.review_orchestrator`` reads server-sourced names (``_store``,
``_run_reviewer``, ``_open_pr``, ``_validate_key``, ...) as bare module
globals, and the test suite patches those names on ``pipeline.server``. A
module-load copy (``from pipeline.server import _run_reviewer``) would freeze
the real function into the importing module and every
``monkeypatch.setattr(pipeline.server, "NAME", ...)`` would be silently
ignored. ``_ServerRef`` re-reads ``pipeline.server.<name>`` on every access,
so patches land and the value is always the current one.

WHY this module re-exports rather than declaring its own class:
``pipeline.service._ServerRef`` is the shared implementation of this proxy and
already carries every dunder the review body needs — ``__gt__``, ``__ge__``,
``__contains__``, ``__iter__``, ``__len__``, ``__eq__``, ... — so the review
body's ``files_changed > REVIEWER_AUTO_FIX_MAX_FILES``,
``fallback_mode in _LOCAL_BACKEND_NAMES`` and
``inconclusive >= REVIEW_INCONCLUSIVE_MAX`` comparisons all resolve against
the live ``pipeline.server`` value. ``pipeline.dispatch`` reuses the same
class the same way. ``pipeline.live_ref.LiveRef`` is the other shared proxy,
but it is not a drop-in here: it lacks those comparison and container
dunders. Re-exporting keeps one implementation instead of another copy.
"""

from .service import _ServerRef

__all__ = ["_ServerRef"]
