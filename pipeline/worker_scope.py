"""Bind the namespace the dispatcher sent to an agent-loop process.

The agent loop runs tool calls in its own process, so the namespace the
dispatcher is already bound to has to travel with it as
``PIPELINE_WORKER_NAMESPACE``. Without the binding, those calls write the
default partition. An absent variable keeps the default, exactly as before.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def worker_namespace() -> Iterator[None]:
    """Bind ``PIPELINE_WORKER_NAMESPACE`` for the run, when it is set."""
    raw = os.environ.get("PIPELINE_WORKER_NAMESPACE", "").strip()
    if not raw:
        yield
        return
    from pipeline.actor import bind_namespace
    from pipeline.store_records import validate_namespace

    validate_namespace(raw)
    with bind_namespace(raw):
        yield
