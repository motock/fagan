"""The namespace resolver seam: which namespaces a principal may reach.

A resolver answers one question: for this principal, which namespaces, and with
which roles in each? With no plugin configured the answer is exactly one entry,
the store's default namespace carrying the principal's own roles, which is what
every store already serves.

An operator may replace the resolver with
``PIPELINE_NAMESPACE_RESOLVER_PLUGIN=module:attr``. Loading follows the
authorizer seam in :mod:`app.policy`: the module allowlist is required, the spec
is validated before anything is imported, and the built object must provide
``accesses``. A bad spec, a disallowed module or a wrong shape raises
:class:`~app.plugin_loader.PluginError` at activation instead of falling back.

Results are checked on every call. An empty result, a duplicate namespace or an
entry of the wrong type is refused rather than read as an answer.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Protocol

from app.identity import ROLES, Principal
from app.plugin_loader import (
    PluginError,
    load_plugin,
    plugin_spec,
    require_allowlist,
    require_methods,
)
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    validate_namespace,
)

logger = logging.getLogger("pipeline.namespace_resolver")

#: The operator-controlled variable that selects the namespace resolver seam.
NAMESPACE_RESOLVER_PLUGIN_ENV = "PIPELINE_NAMESPACE_RESOLVER_PLUGIN"

_SEAM = "namespace_resolver"


class NamespaceResolverError(Exception):
    """A namespace access or a resolver result is malformed.

    Messages name the failure kind and never echo the offending value.
    """


@dataclass(frozen=True)
class NamespaceAccess:
    """One namespace a principal may reach, with the roles it holds there."""

    namespace: str
    roles: frozenset[str]

    def __post_init__(self) -> None:
        try:
            validate_namespace(self.namespace)
        except InvalidRecordKeyError:
            raise NamespaceResolverError("invalid namespace") from None
        if not isinstance(self.roles, frozenset):
            raise NamespaceResolverError("invalid roles: must be a frozenset")
        if not self.roles <= ROLES:
            raise NamespaceResolverError("invalid role: not a role defined by ADR 0009")


class NamespaceResolver(Protocol):
    """Map a principal to the namespaces it may reach, with its roles in each."""

    def accesses(self, principal: Principal) -> tuple[NamespaceAccess, ...]:
        """Return one entry per namespace. An empty tuple is refused by callers."""
        ...


class DefaultNamespaceResolver:
    """With no plugin configured: the default namespace with the principal's roles."""

    def accesses(self, principal: Principal) -> tuple[NamespaceAccess, ...]:
        return (NamespaceAccess(DEFAULT_NAMESPACE, frozenset(principal.roles)),)


def checked_accesses(
    resolver: NamespaceResolver, principal: Principal
) -> tuple[NamespaceAccess, ...]:
    """Return ``resolver``'s answer for ``principal`` after checking its shape.

    The answer must be a non-empty tuple of :class:`NamespaceAccess` with each
    namespace appearing once. Any other result raises rather than being read as
    an empty or partial answer, and an exception from the resolver propagates.
    """
    result = resolver.accesses(principal)
    if not isinstance(result, tuple):
        raise NamespaceResolverError("resolver result must be a tuple")
    if not result:
        raise NamespaceResolverError("resolver returned no namespaces")
    seen: set[str] = set()
    for access in result:
        if not isinstance(access, NamespaceAccess):
            raise NamespaceResolverError(
                "resolver result entry is not a NamespaceAccess"
            )
        if access.namespace in seen:
            raise NamespaceResolverError("duplicate namespace in resolver result")
        seen.add(access.namespace)
    return result


#: Built plugin resolvers, keyed by spec. Written only after the object passed
#: :func:`app.plugin_loader.require_methods`, so an unvalidated resolver is never
#: handed to a later decision. Tests clear this dict between cases.
_PLUGIN_RESOLVERS: dict[str, NamespaceResolver] = {}
#: Re-entrant: a plugin factory may itself ask for the resolver while it is built.
_RESOLVER_LOCK = threading.RLock()


def active_namespace_resolver() -> NamespaceResolver:
    """Return the resolver for this deployment.

    With ``PIPELINE_NAMESPACE_RESOLVER_PLUGIN`` unset this is
    :class:`DefaultNamespaceResolver`, and nothing is logged or cached. A set
    spec is loaded once and kept for the life of the process, the same way the
    authorizer is. A bad spec raises :class:`~app.plugin_loader.PluginError`
    rather than falling back to the default.
    """
    with _RESOLVER_LOCK:
        for live in _PLUGIN_RESOLVERS.values():
            return live
        spec = plugin_spec(_SEAM)
        if spec is None:
            return DefaultNamespaceResolver()
        # Deny by default: the resolver decides which namespaces a principal
        # reaches, so a tree-writer must not install one without the module
        # allowlist. Raises before anything is imported.
        require_allowlist(_SEAM)
        factory = load_plugin(_SEAM)
        if factory is None:
            # Unreachable outside monkeypatch: both reads take the same stripped
            # variable, so a non-None spec cannot yield no factory.
            raise PluginError(
                f"{NAMESPACE_RESOLVER_PLUGIN_ENV} is set but no namespace resolver "
                "plugin factory could be resolved"
            )
        built = factory()
        # Validate before caching, so an object without ``accesses`` never serves
        # a later decision.
        require_methods(built, ("accesses",), "namespace resolver")
        _PLUGIN_RESOLVERS[spec] = built
        logger.info(
            "namespace resolver plugin loaded",
            extra={
                "plugin_seam": _SEAM,
                "plugin_module": spec.partition(":")[0],
            },
        )
        return built
