#!/usr/bin/env python3
"""Guided pipeline configuration (thin CLI over ``pipeline.setup``).

All setup logic lives in ``pipeline/setup``; this script only parses flags,
checks that the server dependencies are importable, and drives
detect -> build_plan -> run_plan.  ``pipeline.setup`` is imported inside
``main()`` so ``--help`` works before ``scripts/install.sh`` has created the
venv.  ``--repo-root`` is never defaulted: omit it unless naming a target
project.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SKIP_FLAGS = (
    ("--skip-notifications", "notifications"),
    ("--skip-integrations", "integrations"),
    ("--skip-services", "services"),
    ("--skip-claude-assets", "claude_assets"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="configure.py", description="Guided pipeline configuration."
    )
    parser.add_argument("--repo-root", help="target project to manage (optional)")
    parser.add_argument("--data-dir", help="plan/worktree data directory")
    parser.add_argument("--port", type=int, default=8001, help="dashboard port")
    parser.add_argument("--autonomy", choices=("dry-run", "gated", "full"))
    parser.add_argument("--role", help="pipeline role to route")
    parser.add_argument("--provider", help="provider for --role")
    parser.add_argument("--model", help="model for --role")
    parser.add_argument(
        "--set", action="append", default=[], metavar="KEY=VALUE",
        help="set a configuration key (repeatable)",
    )
    for flag, section in _SKIP_FLAGS:
        parser.add_argument(flag, action="append_const", const=section, dest="skip",
                            help=f"skip the {section} section")
    parser.add_argument("--yes", action="store_true",
                        help="accept defaults without prompting")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan without writing anything")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        import httpx  # noqa: F401
        import mcp  # noqa: F401
    except ImportError:
        print(
            "run scripts/install.sh first (it creates .venv and installs the server deps)",
            file=sys.stderr,
        )
        return 2
    try:
        return _configure(args)
    except KeyboardInterrupt:
        return 130
    except EOFError:
        return 0
    except Exception as exc:  # noqa: BLE001 - one-line report, no values leaked
        print(f"configure failed: {type(exc).__name__}", file=sys.stderr)
        return 1


def _configure(args) -> int:
    from pipeline.setup import detect, envfile, wizard

    target = Path(args.repo_root) if args.repo_root else Path.cwd()
    state = detect.collect_state(target)
    state["env"] = envfile.read_env_file(target / wizard.ENV_FILE_NAME)
    plan = wizard.build_plan(args, state=state)
    if plan["errors"]:
        for message in plan["errors"]:
            print(message, file=sys.stderr)
        return 2
    result = wizard.run_plan(plan, state=state)
    print(wizard.render_summary(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
