"""The freshness extras merged into the ``/api/health`` payload.

``/api/health`` answers "is this instance alive, and does its own view of the
world agree with what the daemons report?". This module owns the *additive*
part of that answer: the extra blocks a caller can read without changing the
meaning of the pre-existing keys, plus the verdicts those blocks imply
(``healthy`` and ``degraded``).

The shape is deliberately a small dict, computed per call:

    {"healthy": bool, "degraded": [str, ...], "outbox": {...}}

* ``outbox`` is the aggregate depth/age of the notification outbox spool, from
  :func:`pipeline.outbox_health.outbox_summary` -- counts and one age, never a
  plan name, path or message body.
* ``degraded`` collects verdict strings (currently ``"outbox_stale"``); a
  stale outbox also clears ``healthy`` without dropping verdicts contributed
  by other contributors.
* Nothing here is cached: a health probe must reflect the filesystem as it is
  right now, and reads only -- the outbox probe takes no lock the drain needs
  and creates no state.

Integration point: the dashboard's ``health()`` handler merges this dict into
its response (``health_extras`` is the designated seam for that merge, so the
dashboard file itself never has to grow a freshness concern). Until that merge
lands, importing this module still gives callers the same block directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline import outbox_health


def health_extras(plan_dir: str | Path | None = None) -> dict[str, Any]:
    """Return the freshness extras for ``/api/health``.

    *plan_dir* defaults to :data:`pipeline.paths.PLAN_DIR` -- imported lazily so
    the module stays importable without the full runtime configuration, and so
    a caller (or a test) can point the probe at any spool directory.

    The returned dict is fresh on every call and aggregates only: no plan
    names, file paths or message text appear in it. A missing or unreadable
    spool directory yields ``{"files": 0, "records": 0, "oldest_age_s": None}``
    and leaves ``healthy`` alone -- an outbox we cannot read is a reason to
    look, not a reason to page.
    """
    if plan_dir is None:
        from pipeline import paths

        plan_dir = paths.PLAN_DIR
    result: dict[str, Any] = {"healthy": True, "degraded": []}
    outbox_health.apply_outbox_health(result, plan_dir)
    return result
