"""List the namespaces that hold plans, through an optional store capability.

A record store may offer ``list_namespaces()`` to report every namespace that
holds at least one plan. It is a capability, not a member of the
:class:`pipeline.store_records.RecordStore` Protocol, so a store without it
keeps working: the listing then covers only the bound request namespace.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from pipeline.actor import current_namespace
from pipeline.store_records import RecordStore, validate_namespace

logger = logging.getLogger(__name__)

T = TypeVar('T')


def namespaces_with_plans(records: RecordStore) -> list[str]:
    """Return the sorted namespaces of ``records`` that hold a plan.

    An entry the capability reports that is not a valid namespace raises; it is
    never skipped.
    """
    list_namespaces = getattr(records, 'list_namespaces', None)
    if list_namespaces is None:
        namespace = current_namespace()
        return [namespace] if records.list_plans(namespace) else []
    return sorted(validate_namespace(namespace) for namespace in list_namespaces())


def process_wide_namespaces(records: RecordStore, default: str) -> list[str]:
    """Return the sorted namespaces a process-wide read covers.

    That is ``default`` plus every namespace :func:`namespaces_with_plans`
    reports, de-duplicated. A store without the capability is read as its
    default namespace only, exactly as before, and its plan listing is not
    asked for a second time.
    """
    if getattr(records, 'list_namespaces', None) is None:
        return [default]
    return sorted({default, *namespaces_with_plans(records)})


def read_each_namespace(namespaces: list[str], read: Callable[[str], T]) -> list[T]:
    """Return ``read(namespace)`` for each namespace that reads cleanly.

    A read that raises is skipped and logged by error type only, so one
    namespace cannot hide the others. When namespaces were given and every one
    failed, this raises: an unreadable store must reach the caller as an error,
    never as an empty result.
    """
    results: list[T] = []
    for namespace in namespaces:
        try:
            results.append(read(namespace))
        except Exception as exc:  # noqa: BLE001 - one namespace must not hide the others
            logger.warning(
                'process-wide read skipped a namespace: error_type=%s',
                type(exc).__name__,
            )
    if namespaces and not results:
        raise RuntimeError('no namespace could be read')
    return results


def outbox_summary_across_namespaces(records: Any, default: str) -> dict:
    """Return one outbox aggregate over every namespace a process-wide read covers.

    Each namespace is asked for its own ``outbox_summary``. ``files`` and
    ``records`` are summed; ``oldest_age_s`` is the largest known age, or
    ``None`` when no namespace knows one.
    """
    namespaces = process_wide_namespaces(records, default)
    summaries = read_each_namespace(namespaces, records.outbox_summary)
    ages = [s.get('oldest_age_s') for s in summaries if s.get('oldest_age_s') is not None]
    return {
        'files': sum(s.get('files', 0) for s in summaries),
        'records': sum(s.get('records', 0) for s in summaries),
        'oldest_age_s': max(ages) if ages else None,
    }
