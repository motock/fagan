"""Organization-level context sources from an operator-selected provider.

With ``PIPELINE_ORG_CONTEXT_PLUGIN`` unset no provider is consulted and ingest
reads only ``PIPELINE_CONTEXT_CONFIG``. With a provider selected, it is asked
once at ingest which org sources apply to the bound namespace, and its sources
are merged after the file's, at the same org level.

Core carries no tenancy: the provider decides what a namespace inherits. The
seam is a security boundary: the provider loads only through the plugin
allowlist, and every failure to consult it (an exception, a non-list result, an
invalid source) refuses the ingest with one fixed error string. The sources are
reference data only; they still pass ``validate_context_config`` here and the
dispatch-time secret scan, size caps and digest snapshots.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from app.plugin_loader import (
    load_plugin,
    plugin_spec,
    require_allowlist,
    require_methods,
)
from pipeline.audit import append_host_audit
from pipeline.context_sources import validate_context_config

logger = logging.getLogger("pipeline")

ORG_CONTEXT_PLUGIN_ENV = "PIPELINE_ORG_CONTEXT_PLUGIN"
PROVIDER_ERROR = "org context provider failed"

_SEAM = "org_context"
_HOOK_ERROR = "hook_error"
_INVALID_SOURCES = "invalid_sources"


class OrgContextProvider(Protocol):
    def org_sources(self, namespace: str) -> list[dict]: ...


def plugin_org_sources(namespace: str) -> tuple[list[dict], str | None]:
    """Return ``(sources, error)`` for the provider's org sources in ``namespace``.

    With no provider selected this returns ``([], None)`` and imports nothing.
    An empty list is valid and means no sources. Any failure returns
    ``([], PROVIDER_ERROR)`` and is audited with a reason token only.
    """
    if plugin_spec(_SEAM) is None:
        return [], None
    try:
        require_allowlist(_SEAM)
        provider = load_plugin(_SEAM)()
        require_methods(provider, ("org_sources",), "org context provider")
        result = provider.org_sources(namespace)
        errors = _source_errors(result)
    except Exception as exc:  # noqa: BLE001 - any failure to consult must refuse
        logger.error(
            "org context provider failed; refusing",
            extra={"error_type": type(exc).__name__},
        )
        return [], _refuse(_HOOK_ERROR)
    if errors:
        logger.error(
            "org context provider returned invalid sources; refusing",
            extra={"reason": _INVALID_SOURCES},
        )
        return [], _refuse(_INVALID_SOURCES)
    return result, None


def merge_org_sources(
    file_sources: list[dict], provider_sources: list[dict]
) -> tuple[list[dict], str | None]:
    """Return the file sources then the provider's, or an error on a shared name.

    Names are operator-chosen identifiers, so the error may echo one.
    """
    file_names = {source["name"] for source in file_sources}
    for source in provider_sources:
        if source["name"] in file_names:
            return [], (
                f"org context source name '{source['name']}' is declared by both "
                "PIPELINE_CONTEXT_CONFIG and the org context provider"
            )
    return file_sources + provider_sources, None


def _source_errors(result: Any) -> list[str]:
    if not isinstance(result, list):
        return ["org context provider must return a list"]
    if not result:
        return []
    return validate_context_config({"sources": result})


def _refuse(reason: str) -> str:
    try:
        append_host_audit(
            action="org_context_refused",
            outcome="refused",
            detail={"reason": reason},
        )
    except Exception:  # noqa: BLE001 - a failed audit append never changes the decision
        logger.warning("org context refusal audit append failed")
    return PROVIDER_ERROR
