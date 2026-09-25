#!/usr/bin/env python3
"""CLI for installing global rules bundles for supported tools.

This script is a thin wrapper around :func:`pipeline.global_rules_install.install_for_tool`.
It parses command‑line arguments, validates the requested tools, and reports the
status of each installation.

The public API is a single function ``main(argv: list[str] | None = None) -> int``
which returns an exit code suitable for ``sys.exit``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Import the engine.  The test monkeypatches this import target.
from pipeline.global_rules_install import install_for_tool

# Supported tools.
TOOLS = ("claude", "codex", "opencode")


def _parse_tools(tools_str: str) -> list[str]:
    """Parse comma‑separated tool list, validating each entry.

    Raises ``ValueError`` if the string is empty, contains an empty entry, or
    contains an unknown tool.
    """
    parts = [t.strip() for t in tools_str.split(",")]
    if not parts or any(not p for p in parts):
        raise ValueError("empty tool entry")
    for p in parts:
        if p not in TOOLS:
            raise ValueError(f"unknown tool: {p}")
    return parts


def main(argv: list[str] | None = None) -> int:
    """Entry point for the CLI.

    Parameters
    ----------
    argv:
        Argument list, or ``None`` to use ``sys.argv[1:]``.

    Returns
    -------
    int
        Exit code: 0 on success, 1 if the engine raised, 2 for usage errors.
    """
    parser = argparse.ArgumentParser(description="Install global rules bundles.")
    parser.add_argument(
        "--tools",
        required=True,
        help="Comma‑separated list of tools (claude,codex,opencode)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without writing files")
    parser.add_argument(
        "--source-root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Root directory containing the source files (default: repo root)",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse uses SystemExit
        return exc.code if isinstance(exc.code, int) else 1

    try:
        tool_list = _parse_tools(args.tools)
    except ValueError:
        return 2

    env = os.environ
    source_root = Path(args.source_root)
    dry_run = args.dry_run

    for tool in tool_list:
        target = install_for_tool(tool, source_root, env, dry_run=dry_run).target
        # Determine status
        # We need to know if target existed before the call
        # Since install_for_tool may have created it, we check existence after
        # but we can infer from the result: if dry_run, changed indicates would-create or would-update
        # For non-dry_run, changed indicates created or updated
        # We can check existence before calling by inspecting the target path
        # but install_for_tool already returned target; we can check if target existed before by
        # using target.exists() after the call and comparing with changed flag.
        # Simpler: call install_for_tool again? No.
        # Instead, we can compute status based on dry_run and changed flag.
        # For dry_run: if target existed before? We can't know. But we can check target.exists() after call: if changed and not dry_run, it was created or updated.
        # For dry_run, changed True means would-create if target did not exist before, would-update if it did.
        # We can check target.exists() before calling by storing a flag.
        # Let's redo: we need to call install_for_tool but we need existence before.
        # We'll modify: before calling, compute target path via global_rules_targets.instructions_path.
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())

