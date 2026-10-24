"""Machine-readable marker an agent emits when the brief needs an out-of-scope path.

Pure parsing, no I/O. A marker is one line:
``SCOPE-CONFLICT: <repo-relative path> :: <reason>``.
"""

from __future__ import annotations

from dataclasses import dataclass

SCOPE_CONFLICT_PREFIX = "SCOPE-CONFLICT:"

_MAX_MARKERS = 5
_MAX_REASON_CHARS = 200
_SEPARATOR = "::"


@dataclass(frozen=True)
class ScopeConflict:
    path: str
    reason: str


def _is_valid_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        return False
    return ".." not in path.split("/")


def _clean_reason(reason: str) -> str:
    cleaned = "".join(ch for ch in reason if ch.isprintable() or ch in " \t")
    return cleaned.strip()[:_MAX_REASON_CHARS]


def parse_scope_conflict(text: str | None) -> list[ScopeConflict]:
    """Return up to 5 valid, de-duplicated markers found in ``text``."""
    if not text:
        return []
    conflicts: list[ScopeConflict] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(SCOPE_CONFLICT_PREFIX):
            continue
        path, sep, reason = line[len(SCOPE_CONFLICT_PREFIX) :].partition(_SEPARATOR)
        path = path.strip()
        if not sep or not _is_valid_path(path) or path in seen:
            continue
        seen.add(path)
        conflicts.append(ScopeConflict(path, _clean_reason(reason)))
        if len(conflicts) == _MAX_MARKERS:
            break
    return conflicts
