#!/usr/bin/env python3
"""Opt-in CLI that installs the global rules bundle for supported agent CLIs.

Thin wrapper around :func:`pipeline.global_rules_install.install_for_tool`.
Nothing is written unless ``--tools`` is passed, so importing this module or
running it without arguments is always safe.

Exit codes:
    0  success (including ``--dry-run``)
    1  the install engine raised (message on stderr, no traceback)
    2  usage error (missing/invalid ``--tools``)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from pipeline import global_rules_targets
from pipeline.global_rules_install import install_for_tool

TOOLS = ("claude", "codex", "opencode")

_WARNING_INDENT = "  "


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser. ``--tools`` is required: opt-in by default."""
    parser = argparse.ArgumentParser(
        description="Install the global rules bundle for agent CLIs.",
    )
    parser.add_argument(
        "--tools",
        required=True,
        help="comma-separated tools to install: claude,codex,opencode",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would happen without writing any file",
    )
    parser.add_argument(
        "--source-root",
        default=str(Path(__file__).resolve().parent.parent),
        help="repository root holding global-rules/ (default: this repo)",
    )
    return parser


def _parse_tools(raw: str) -> list[str]:
    """Split *raw* on commas and validate every entry.

    The whole list is validated before any install runs, so an empty or
    unknown entry can never leave a partially installed bundle behind.

    Raises ``ValueError`` for an empty entry or an unknown tool.
    """
    tools = [entry.strip() for entry in raw.split(",")]
    if not tools or any(not entry for entry in tools):
        raise ValueError("--tools must not contain an empty entry")
    unknown = [entry for entry in tools if entry not in TOOLS]
    if unknown:
        raise ValueError(f"unknown tool(s): {', '.join(unknown)}")
    return tools


def _result_status(result: object, *, existed: bool, dry_run: bool) -> str:
    """Derive the status word from the result's ``changed`` flag plus whether
    the target existed beforehand."""
    if not getattr(result, "changed", False):
        return "unchanged"
    if dry_run:
        return "would-update" if existed else "would-create"
    return "updated" if existed else "created"


def _install_env() -> dict[str, str]:
    """Return the environment mapping forwarded to the install engine.

    A copy of the live ``os.environ`` taken at call time (never at import
    time, so test/runner overrides are seen), forwarded unchanged. Every
    variable that selects where a tool reads its instructions
    (``CLAUDE_CONFIG_DIR``, ``CODEX_HOME``, ``OPENCODE_CONFIG_DIR`` and
    ``XDG_CONFIG_HOME``) must reach the engine, or the bundle lands
    somewhere the tool never looks.
    """
    return dict(os.environ)


def main(argv: list[str] | None = None) -> int:
    """Install the global rules bundle for each requested tool.

    Returns 0 on success, 1 when the engine raises, 2 on usage errors.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse reports usage errors itself
        code = exc.code
        return code if isinstance(code, int) else 0

    try:
        tools = _parse_tools(args.tools)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    env = _install_env()
    source_root = Path(args.source_root)
    dry_run = args.dry_run

    for tool in tools:
        try:
            # The target pre-check lives inside the guard too: resolving the
            # target can itself raise (missing HOME, relative config-dir
            # override) and must surface as exit 1, never a traceback.
            existed = global_rules_targets.instructions_path(tool, env).exists()
            result = install_for_tool(
                tool,
                source_root=source_root,
                env=env,
                dry_run=dry_run,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            # Engine failure: message only, no traceback, no extra paths.
            print(str(exc), file=sys.stderr)
            return 1
        status = _result_status(result, existed=existed, dry_run=dry_run)
        print(f"{tool}: {result.target} ({status})")
        if result.warning:
            print(f"{_WARNING_INDENT}{result.warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
