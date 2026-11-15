"""List the namespaces that hold plans, through an optional store capability.

A record store may offer ``list_namespaces()`` to report every namespace that
holds at least one plan. It is a capability, not a member of the
:class:`pipeline.store_records.RecordStore` Protocol, so a store without it
keeps working: the listing then covers only the bound request namespace.
"""

from __future__ import annotations

from pipeline.actor import current_namespace
from pipeline.store_records import RecordStore, validate_namespace


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
