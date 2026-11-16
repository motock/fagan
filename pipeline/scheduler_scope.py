"""Which namespaces one scheduler instance advances, read from its environment.

``PIPELINE_SCHEDULER_NAMESPACES_ONLY`` advances just the listed namespaces.
``PIPELINE_SCHEDULER_NAMESPACES_SKIP`` advances everything except the listed
namespaces. Each is a comma-separated list; entries are whitespace-trimmed and
the literal ``default`` names the default namespace.

Unset or empty means no scope. A malformed scope fails closed: both variables
set, a blank entry, or an entry that is not a valid namespace raises
:class:`SchedulerScopeError`, and the caller advances nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    validate_namespace,
)

ONLY_VAR = "PIPELINE_SCHEDULER_NAMESPACES_ONLY"
SKIP_VAR = "PIPELINE_SCHEDULER_NAMESPACES_SKIP"

_DEFAULT_TOKEN = "default"
_SEPARATOR = ","


class SchedulerScopeError(ValueError):
    """The scheduler scope is malformed; the message never echoes the value."""


@dataclass(frozen=True)
class SchedulerScope:
    """A namespace scope: ``only`` admits exactly its members, else ``skip`` excludes."""

    only: tuple[str, ...] | None = None
    skip: frozenset[str] = frozenset()

    def allows(self, namespace: str) -> bool:
        if self.only is not None:
            return namespace in self.only
        return namespace not in self.skip


def _parse_list(raw: str) -> tuple[str, ...]:
    """Return the namespaces of one comma-separated list, in order, without repeats."""
    namespaces: list[str] = []
    for part in raw.split(_SEPARATOR):
        entry = part.strip()
        if not entry:
            raise SchedulerScopeError("blank entry")
        if entry == _DEFAULT_TOKEN:
            entry = DEFAULT_NAMESPACE
        try:
            validate_namespace(entry)
        except InvalidRecordKeyError as exc:
            raise SchedulerScopeError("invalid namespace") from exc
        if entry not in namespaces:
            namespaces.append(entry)
    return tuple(namespaces)


def load_scope(environ: Mapping[str, str]) -> SchedulerScope | None:
    """Return the scope the environment names, or ``None`` when neither variable is set."""
    only_raw = environ.get(ONLY_VAR, "")
    skip_raw = environ.get(SKIP_VAR, "")
    only_set = only_raw != ""
    skip_set = skip_raw != ""
    if only_set and skip_set:
        raise SchedulerScopeError("both scope variables are set")
    if only_set:
        return SchedulerScope(only=_parse_list(only_raw))
    if skip_set:
        return SchedulerScope(skip=frozenset(_parse_list(skip_raw)))
    return None
