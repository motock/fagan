"""The view-scope seam: which namespaces a principal may see in plan views.

A view-scope hook answers one question: for this principal, which namespaces
may its plan views show? The answer is either a ``frozenset`` of namespace
names or :data:`ALL`. With no plugin configured the answer is the namespaces
the namespace resolver reports for the principal, which today is the default
namespace alone, so nothing a view shows changes.

An operator may replace the hook with ``PIPELINE_VIEW_SCOPE_PLUGIN=module:attr``.
Loading follows the namespace resolver seam: the module allowlist is required,
the spec is validated before anything is imported, and the built object must
provide ``visible``. A bad spec or a wrong shape raises
:class:`~app.plugin_loader.PluginError` at activation instead of falling back.

Results are checked on every call. An empty answer, a non-frozenset answer or
an invalid namespace is refused with :class:`ViewScopeError`, and an exception
from the hook propagates. A caller that gets an error must deny the view; no
path turns a failure into an open or an empty view.
"""

from __future__ import annotations

import threading
from typing import Protocol

from app.identity import Principal
from app.plugin_loader import (
    PluginError,
    load_plugin,
    plugin_env_name,
    plugin_spec,
    require_allowlist,
    require_methods,
)
from pipeline.namespace_resolver import active_namespace_resolver, checked_accesses
from pipeline.store_records import InvalidRecordKeyError, validate_namespace

#: The operator-controlled variable that selects the view-scope seam.
VIEW_SCOPE_PLUGIN_ENV = plugin_env_name("view_scope")

_SEAM = "view_scope"


class ViewScopeError(Exception):
    """A view-scope answer is malformed.

    Messages name the failure kind and never echo the offending value.
    """


class _AllNamespaces:
    """The answer that sees every namespace. A single shared instance: :data:`ALL`."""

    def __repr__(self) -> str:
        return "ALL"


#: The view-scope answer for "every namespace".
ALL = _AllNamespaces()


class ViewScope(Protocol):
    """Map a principal to the namespaces its plan views may show."""

    def visible(self, principal: Principal) -> frozenset[str] | _AllNamespaces:
        """Return a frozenset of namespaces, or :data:`ALL`. Anything else is refused."""
        ...


class DefaultViewScope:
    """With no plugin configured: the namespaces the namespace resolver reports."""

    def visible(self, principal: Principal) -> frozenset[str]:
        accesses = checked_accesses(active_namespace_resolver(), principal)
        return frozenset(access.namespace for access in accesses)


def checked_visible(
    scope: ViewScope, principal: Principal
) -> frozenset[str] | _AllNamespaces:
    """Return ``scope``'s answer for ``principal`` after checking its shape.

    The answer must be :data:`ALL` or a non-empty frozenset of valid namespace
    names. Anything else raises :class:`ViewScopeError` instead of being read as
    an empty or partial view.
    """
    result = scope.visible(principal)
    if result is ALL:
        return ALL
    if not isinstance(result, frozenset):
        raise ViewScopeError("view scope result must be a frozenset or ALL")
    if not result:
        raise ViewScopeError("view scope returned no namespaces")
    for namespace in result:
        try:
            validate_namespace(namespace)
        except InvalidRecordKeyError:
            raise ViewScopeError("invalid namespace in view scope result") from None
    return result


#: Built plugin scopes, keyed by spec. Written only after the object passed
#: :func:`app.plugin_loader.require_methods`. Tests clear this dict between cases.
_PLUGIN_SCOPES: dict[str, ViewScope] = {}
#: Re-entrant: a plugin factory may itself ask for the scope while it is built.
_SCOPE_LOCK = threading.RLock()


def active_view_scope() -> ViewScope:
    """Return the view scope for this deployment.

    With ``PIPELINE_VIEW_SCOPE_PLUGIN`` unset this is :class:`DefaultViewScope`.
    A set spec is loaded once per spec and kept for the life of the process. A
    bad spec raises :class:`~app.plugin_loader.PluginError` rather than falling
    back to the default.
    """
    with _SCOPE_LOCK:
        spec = plugin_spec(_SEAM)
        if spec is None:
            return DefaultViewScope()
        cached = _PLUGIN_SCOPES.get(spec)
        if cached is not None:
            return cached
        # Deny by default: the scope decides which namespaces a view shows, so a
        # tree-writer must not install one without the module allowlist.
        require_allowlist(_SEAM)
        factory = load_plugin(_SEAM)
        if factory is None:
            # Unreachable outside monkeypatch: both reads take the same stripped
            # variable, so a non-None spec cannot yield no factory.
            raise PluginError(
                f"{VIEW_SCOPE_PLUGIN_ENV} is set but no view scope plugin factory "
                "could be resolved"
            )
        built = factory()
        require_methods(built, ("visible",), "view scope")
        _PLUGIN_SCOPES[spec] = built
        return built


def can_view(principal: Principal, namespace: str) -> bool:
    """Return whether *principal*'s plan views may show *namespace*.

    An invalid namespace raises :class:`~pipeline.store_records.InvalidRecordKeyError`.
    A namespace outside the scope returns ``False``. A failing scope raises, so
    the caller denies the view.
    """
    validate_namespace(namespace)
    visible = checked_visible(active_view_scope(), principal)
    # checked_visible returns ALL or a frozenset, so anything else is ALL.
    return not isinstance(visible, frozenset) or namespace in visible
