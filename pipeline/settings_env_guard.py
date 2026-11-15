"""Refuse to start while a retired tenant-scoped environment variable is set.

ADR 0015 moved the eight tenant settings into the settings Store. Their legacy
``PIPELINE_*`` names are no longer read at runtime, so a deployment that still
sets one would silently run on the Store value (or the safe default) while the
operator believes the variable is in force. The launch paths call
``refuse_retired_env`` before they serve, so the process exits non-zero and the
supervisor shows the operator the names to migrate.

A variable is refused when it is present with any value, including the old
default and an empty string. Refusal output names the variable and its
replacement key only; it never echoes a value. A failure inside the check
refuses start too: the check fails closed.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any

from pipeline.settings_registry import REGISTRY


def retired_env_present(environ: Mapping[str, str]) -> list[dict[str, str]]:
    """Return ``{name, replacement}`` for each retired variable present in *environ*."""
    # A key with no retired name has legacy_env "" and is never reported.
    return [
        {"name": setting.legacy_env, "replacement": setting.name}
        for setting in REGISTRY.values()
        if setting.legacy_env and setting.legacy_env in environ
    ]


def _refusal_line(finding: dict[str, str]) -> str:
    return (
        f"{finding['name']} is retired; set {finding['replacement']} with set_setting "
        "(or run python -m pipeline.settings_seed first). See ADR 0015."
    )


def refuse_retired_env(environ: Mapping[str, str] | None = None) -> None:
    """Exit with status 1, one stderr line per retired variable, if any is set.

    Returns ``None`` when no retired variable is present. An exception raised
    while checking is reported by its class name alone and also exits 1.
    """
    if environ is None:
        environ = os.environ
    try:
        findings: list[dict[str, Any]] = retired_env_present(environ)
    except Exception as error:  # noqa: BLE001 - fail closed on any failure to check
        # The class name only: an exception message can carry environment data.
        print(
            f"settings_env_guard: ERROR could not check retired settings variables "
            f"({type(error).__name__}); refusing to start",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    if not findings:
        return
    for finding in findings:
        print(_refusal_line(finding), file=sys.stderr)
    raise SystemExit(1)
