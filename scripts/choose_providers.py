#!/usr/bin/env python3
"""Interactive per-role provider picker for the pipeline's role routing.

For every pipeline role this renders the role's CURRENT provider/model, the
provenance source that setting came from, and the numbered provider/model
pairs the model registry declares.  The operator types an option number to
switch, or presses Enter to keep the current setting.

Every applied change goes through ``PipelineService.set_role_default`` - the
security-sensitive entry point that validates the role, provider, and model
against the registry and writes ``model_registry.json`` atomically.  This
script contains no persistence code of its own and never touches the
registry file.

Non-interactive safety: when stdin is not a TTY the script prints the current
routing plus guidance for changing it non-interactively, makes no writes, and
exits 0 without ever prompting - an installer that pipes stdin
(scripts/install.sh) must never hang here waiting on input.

Injection seam (driven by tests/unit/test_choose_providers_cli.py):
``input_fn`` stands in for stdin, ``setter`` stands in for
``PipelineService.set_role_default``, and ``effective_config`` / ``registry``
stand in for the on-disk config and model registry.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.role_registry import load_registry
from pipeline.config_provenance import PIPELINE_ROLES
from pipeline.provider_choice import build_choice_model
from pipeline.service import PipelineService


def _default_setter(role: str, provider: str | None, model: str | None) -> dict:
    """Apply one change through the validating, atomically-writing service."""
    return PipelineService().set_role_default(role, provider, model)


def _stdin_is_tty() -> bool:
    """True only when stdin is genuinely interactive.

    Both spellings are consulted (the tests patch ``sys.stdin`` and
    ``os.isatty`` together) and the answer must be unanimous: a captured or
    piped stdin counts as non-interactive, so the picker can never block an
    installer waiting on input that will never arrive.
    """
    try:
        if not sys.stdin.isatty():
            return False
    except (AttributeError, ValueError):
        pass
    try:
        return bool(os.isatty(0))
    except (OSError, ValueError):
        return False


def _build_parser() -> argparse.ArgumentParser:
    roles = ", ".join(PIPELINE_ROLES)
    parser = argparse.ArgumentParser(
        prog="choose_providers.py",
        description=(
            "Interactively pick the provider/model that serves each pipeline "
            f"role ({roles})."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "render every role and accept choices, but apply nothing: "
            "set_role_default is never called and model_registry.json is "
            "left untouched"
        ),
    )
    parser.add_argument(
        "--role",
        metavar="NAME",
        help=(
            "configure a single pipeline role instead of all of them "
            f"(one of: {roles})"
        ),
    )
    return parser


def _format_current(current: dict) -> str:
    """Human line for a role's routing, e.g. ``claude/opus  [source: registry]``."""
    provider = current.get("provider")
    model = current.get("model")
    if provider and model:
        setting = f"{provider}/{model}"
    else:
        setting = provider or model or "(not set)"
    return f"{setting}  [source: {current.get('source') or 'default'}]"


def _render_role(choice: dict) -> None:
    """One section: the role, its current routing + provenance, numbered options."""
    current = choice["current"]
    print(f"\n=== {choice['role']} ===")
    print(f"  current: {_format_current(current)}")
    if choice.get("error"):
        print(f"  config warning: {choice['error']}")
    options = choice["options"]
    if not options:
        print("  (the registry declares no provider/model options)")
        return
    for index, option in enumerate(options, start=1):
        is_current = (
            option["provider"] == current.get("provider")
            and option["model"] == current.get("model")
        )
        marker = "   <- current" if is_current else ""
        print(f"  {index}. {option['provider']}/{option['model']}{marker}")


def _prompt_choice(
    choice: dict, input_fn: Callable[[str], str]
) -> tuple[str, str] | None:
    """Prompt until the answer is empty (keep current) or a valid option number.

    Non-numeric and out-of-range answers re-prompt the SAME role; nothing is
    applied until a valid number is chosen.  Returns the chosen
    ``(provider, model)``, or None to keep the current setting.
    """
    options = choice["options"]
    if not options:
        return None
    high = len(options)
    while True:
        answer = input_fn(f"  choose 1-{high}, Enter keeps current: ").strip()
        if answer == "":
            return None
        try:
            picked = int(answer)
        except ValueError:
            print(f"  {answer!r} is not a number: enter 1-{high} or press Enter.")
            continue
        if not 1 <= picked <= high:
            print(f"  {picked} is out of range: enter 1-{high} or press Enter.")
            continue
        option = options[picked - 1]
        return option["provider"], option["model"]


def _print_non_interactive_routing(choices: list[dict], roles: list[str]) -> None:
    """Current routing plus non-interactive guidance; no prompting, no writes."""
    print("stdin is not a TTY: running non-interactively, nothing was changed.")
    print("Current role routing:")
    for choice in choices:
        if choice["role"] in roles:
            print(f"  {choice['role']}: {_format_current(choice['current'])}")
    print()
    print("To change this routing non-interactively, call the validating")
    print("service entry point directly, e.g. from a script:")
    print()
    print("  from pipeline.service import PipelineService")
    print("  PipelineService().set_role_default(role, provider, model)")
    print()
    print("Role defaults persist in model_registry.json; set_role_default")
    print("validates each choice against the registry before writing it.")
    print("Or re-run this picker in a terminal: python scripts/choose_providers.py")


def _join(roles: list[str]) -> str:
    return ", ".join(roles) if roles else "(none)"


def _print_summary(
    changed: list[str],
    unchanged: list[str],
    would_change: list[str],
    *,
    dry_run: bool,
) -> None:
    """Which roles changed (or would have) and which were left alone."""
    print()
    print("=== summary ===")
    if dry_run:
        print(f"  dry run: nothing written; would change: {_join(would_change)}")
    else:
        print(f"  changed: {_join(changed)}")
    print(f"  unchanged: {_join(unchanged)}")


def main(
    argv: list[str] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    setter: Callable[[str, str | None, str | None], dict] = _default_setter,
    effective_config: dict | None = None,
    registry: dict | None = None,
) -> int:
    """Render the per-role picker and apply choices via ``set_role_default``.

    ``input_fn`` replaces stdin and ``setter`` replaces
    ``PipelineService.set_role_default`` so tests can drive the picker
    without real input or a real registry write.  ``effective_config`` and
    ``registry`` replace the on-disk config and model registry; when
    omitted, both are loaded from disk.  Returns a process exit code.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: --help or a usage error
        code = exc.code
        return code if isinstance(code, int) else 0

    # Validate --role before anything interactive or any disk access.
    if args.role is not None and args.role not in PIPELINE_ROLES:
        print(
            f"error: unknown role {args.role!r}; valid roles are: "
            f"{', '.join(PIPELINE_ROLES)}",
            file=sys.stderr,
        )
        return 2

    if effective_config is None or registry is None:
        service = PipelineService()
        if effective_config is None:
            effective_config = service.get_effective_config()
        if registry is None:
            registry = load_registry()

    choices = build_choice_model(effective_config, registry)
    by_role = {choice["role"]: choice for choice in choices}
    roles = [args.role] if args.role is not None else list(PIPELINE_ROLES)

    # Non-interactive safety: never block on input that will never come.
    if not _stdin_is_tty():
        _print_non_interactive_routing(choices, roles)
        return 0

    print("Pipeline role provider picker")
    if args.dry_run:
        print("Dry run: choices are shown but nothing is applied.")

    changed: list[str] = []
    would_change: list[str] = []
    for role in roles:
        choice = by_role[role]
        _render_role(choice)
        try:
            picked = _prompt_choice(choice, input_fn)
        except EOFError:
            print()
            print(
                "  input closed early; the remaining roles keep their"
                " current setting."
            )
            break
        if picked is None:
            continue
        provider, model = picked
        if args.dry_run:
            print(f"  dry run: {role} would become {provider}/{model} (not applied)")
            would_change.append(role)
            continue
        result = setter(role, provider, model)
        if isinstance(result, dict) and result.get("ok"):
            changed.append(role)
            print(f"  applied: {role} -> {provider}/{model}")
        else:
            error = result.get("error") if isinstance(result, dict) else result
            print(f"  error: {role} was not changed: {error}")

    unchanged = [
        role for role in roles if role not in changed and role not in would_change
    ]
    _print_summary(changed, unchanged, would_change, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())