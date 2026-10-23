"""Pure validation and normalization of the plan-level ``context`` block.

See docs/plans/EXTERNAL_CONTEXT_SOURCES_PLAN.md §4.2 and §4.9. No I/O here.
"""

import re

SUPPORTED_PROVIDERS = ("dir",)
SUPPORTED_MODES = ("prompt",)
SUPPORTED_PHASES = ("dispatch", "review", "security_review")
SUPPORTED_AUTHORITIES = ("reference", "policy")

DEFAULT_MAX_CHARS = 20000
HARD_MAX_CHARS = 100000
DEFAULT_MAX_TOTAL_CHARS = 60000
DEFAULT_INCLUDE = ("**/*",)

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PLAN_KEYS = {"sources", "max_total_chars"}
_SOURCE_KEYS = {
    "name", "provider", "uri", "include", "exclude", "phases", "mode",
    "max_chars", "authority", "priority", "required",
}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _has_dotdot(path: str) -> bool:
    return ".." in path.split("/")


def _validate_globs(label: str, patterns: object) -> list[str]:
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        return [f"{label} must be a list of strings"]
    errors = []
    for pattern in patterns:
        if pattern.startswith("/") or _has_dotdot(pattern):
            errors.append(f"{label} pattern {pattern!r} must be relative with no '..' segment")
    return errors


def _validate_source(label: str, source: object) -> list[str]:
    if not isinstance(source, dict):
        return [f"{label} must be an object"]
    errors = [f"{label}: unknown key {key!r}" for key in sorted(source.keys() - _SOURCE_KEYS, key=str)]

    name = source.get("name")
    if not isinstance(name, str) or not _NAME_PATTERN.match(name):
        errors.append(f"{label}: name must match {_NAME_PATTERN.pattern}")

    if source.get("provider") not in SUPPORTED_PROVIDERS:
        errors.append(f"{label}: provider must be one of {list(SUPPORTED_PROVIDERS)}")

    uri = source.get("uri")
    if not isinstance(uri, str) or not uri.startswith("/") or _has_dotdot(uri):
        errors.append(f"{label}: uri must be a non-empty absolute path with no '..' segment")

    phases = source.get("phases")
    if not isinstance(phases, list) or not phases:
        errors.append(f"{label}: phases must be a non-empty list")
    else:
        for phase in phases:
            if phase not in SUPPORTED_PHASES:
                errors.append(
                    f"{label}: unsupported phase {phase!r}; supported: {list(SUPPORTED_PHASES)}"
                )

    if source.get("mode", "prompt") not in SUPPORTED_MODES:
        errors.append(f"{label}: mode must be one of {list(SUPPORTED_MODES)}")

    for key in ("include", "exclude"):
        if key in source:
            errors.extend(_validate_globs(f"{label}: {key}", source[key]))

    max_chars = source.get("max_chars", DEFAULT_MAX_CHARS)
    if not _is_int(max_chars) or not 1 <= max_chars <= HARD_MAX_CHARS:
        errors.append(f"{label}: max_chars must be an integer between 1 and {HARD_MAX_CHARS}")

    if source.get("authority", "reference") not in SUPPORTED_AUTHORITIES:
        errors.append(f"{label}: authority must be one of {list(SUPPORTED_AUTHORITIES)}")

    if not _is_int(source.get("priority", 0)):
        errors.append(f"{label}: priority must be an integer")

    if not isinstance(source.get("required", True), bool):
        errors.append(f"{label}: required must be a boolean")

    return errors


def validate_context_config(context: object) -> list[str]:
    """Return human-readable errors for a plan ``context`` block; empty means valid."""
    if not isinstance(context, dict):
        return ["context must be an object"]
    errors = [f"context: unknown key {key!r}" for key in sorted(context.keys() - _PLAN_KEYS, key=str)]

    max_total = context.get("max_total_chars", DEFAULT_MAX_TOTAL_CHARS)
    if not _is_int(max_total) or max_total < 1:
        errors.append("context: max_total_chars must be a positive integer")

    sources = context.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("context: sources must be a non-empty list")
        return errors

    seen: set[str] = set()
    for index, source in enumerate(sources):
        errors.extend(_validate_source(f"sources[{index}]", source))
        name = source.get("name") if isinstance(source, dict) else None
        if isinstance(name, str):
            if name in seen:
                errors.append(f"sources[{index}]: duplicate name {name!r}")
            seen.add(name)
    return errors


def normalize_context_sources(context: dict) -> list[dict]:
    """Return fully-defaulted source entries in declaration order.

    Raises ValueError, listing every problem, if the context is invalid.
    """
    errors = validate_context_config(context)
    if errors:
        raise ValueError("invalid context: " + "; ".join(errors))
    return [
        {
            "name": source["name"],
            "provider": source["provider"],
            "uri": source["uri"],
            "include": list(source.get("include", DEFAULT_INCLUDE)),
            "exclude": list(source.get("exclude", [])),
            "phases": list(source["phases"]),
            "mode": source.get("mode", "prompt"),
            "max_chars": source.get("max_chars", DEFAULT_MAX_CHARS),
            "authority": source.get("authority", "reference"),
            "priority": source.get("priority", 0),
            "required": source.get("required", True),
        }
        for source in context["sources"]
    ]
