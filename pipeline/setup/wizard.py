"""Orchestrate the guided setup into an idempotent, dry-runnable plan.

Three public functions (see ``__all__``):

``build_plan``
    Turns the CLI arguments plus the detected machine state into a plan: which
    sections are enabled, which ``.pipeline.env`` keys change, which existing
    values are preserved, and which routing choices to apply. It performs no
    filesystem I/O and prints nothing; in interactive mode the only input it
    consumes is the injected ``ask`` callable.

``run_plan``
    The single writer. It merges the preserved values with the planned changes,
    writes ``.pipeline.env`` once, creates the plan/worktree directories, then
    applies routing, installs the Claude assets, and renders/starts the
    services. Every host effect is an injectable callable, so a test never
    touches the real machine. A failure in one section is recorded in
    ``errors`` and the remaining sections still run.

``render_summary``
    Deterministic, secret-free text describing what changed, what was skipped,
    what failed, and what to do next.

Design rules, all load-bearing:

* ``build_plan`` is pure. It reads ``state`` (already collected by
  :mod:`pipeline.setup.detect` plus the parsed ``.pipeline.env``) and never
  opens a file, so a dry run cannot leave a trace.
* Every value that reaches ``plan["changes"]`` — schema defaults included —
  goes through :func:`pipeline.setup.schema.validate_value`, and the
  *normalized* value is stored. A path is therefore absolute and a bool is
  ``"1"``/``"0"``.
* A secret is never echoed. Error text may name the key; the value is never
  rendered, logged, returned, or summarised.
* Nothing is raised at the caller for a host condition. Bad input becomes an
  entry in ``plan["errors"]``; a failing applier becomes an entry in
  ``result["errors"]``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from . import claude_setup, envfile, providers, schema, services, ui
from .policy import describe_autonomy

__all__ = ["build_plan", "render_summary", "run_plan"]

#: The fixed order sections are planned, applied, and reported in.
SECTION_ORDER: tuple[str, ...] = (
    "core",
    "routing",
    "policy",
    "notifications",
    "integrations",
    "claude_assets",
    "services",
)

#: Sections the schema owns; the other two are wizard-only.
_SCHEMA_SECTIONS = frozenset(schema.SECTIONS)

#: The default dashboard port. Never 8000: that is the dev server's port.
DEFAULT_PORT = 8001

#: The routing preset a fresh non-interactive install applies.
DEFAULT_PRESET = "claude"

#: The provider/model the ``claude`` preset needs to be declared.
_PRESET_PROVIDER = "claude"
_PRESET_MODEL = "sonnet"

#: The env file the plan writes, relative to the managed repository.
ENV_FILE_NAME = ".pipeline.env"

#: Rendered in place of a secret value, everywhere.
MASK = "***"

#: The manual MCP registration command, shown when the automatic one failed.
MCP_MANUAL_COMMAND = (
    "claude mcp add -s user pipeline <python> <repo>/server.py"
)

#: The two next steps every summary ends with.
RESTART_STEP = "Restart Claude Code so it picks up the registered server."
TEST_STEP = "Run .venv/bin/python -m pytest -q"

#: Policy keys the "advanced gates" branch owns. Declining keeps them at their
#: schema default (or unset, for the escalation pair).
_ADVANCED_POLICY_KEYS: tuple[str, ...] = (
    "PIPELINE_DAILY_REQUEST_THRESHOLD",
    "PIPELINE_WEEKLY_REQUEST_THRESHOLD",
    "PIPELINE_PAUSE_THRESHOLD",
    "PIPELINE_RESUME_THRESHOLD",
    "PIPELINE_ESCALATION_BACKEND",
    "PIPELINE_ESCALATION_MODEL",
    "PIPELINE_SECURITY_AUDIT_EVERY_DAYS",
    "PIPELINE_SECURITY_AUDIT_EVERY_COMMITS",
)

#: Keys that stay unset on a fresh install even though the schema has a value.
_NEVER_DEFAULTED: frozenset[str] = frozenset(
    {"PIPELINE_ESCALATION_BACKEND", "PIPELINE_ESCALATION_MODEL"}
)

#: The curated interactive prompts, per section, in the order they are asked.
_CORE_PROMPTS: tuple[str, ...] = ("PLAN_DIR", "REPO_ROOT")
_ROUTING_PROMPTS: tuple[str, ...] = ("preset",)
_POLICY_PROMPTS: tuple[str, ...] = (
    "PIPELINE_AUTONOMY",
    "PIPELINE_RISK_THRESHOLD",
    "PIPELINE_MAX_CONCURRENT_AGENTS",
)
_NOTIFY_PROMPTS: tuple[str, ...] = (
    "PIPELINE_NOTIFY_OUTBOX_ENABLED",
    "PIPELINE_NOTIFY_EMAIL_ENABLED",
)
_INTEGRATION_PROMPTS: tuple[str, ...] = ("PIPELINE_TICKET_PROVIDER",)

#: The email keys prompted once email delivery is enabled.
_EMAIL_PROMPTS: tuple[str, ...] = (
    "PIPELINE_NOTIFY_EMAIL_HOST",
    "PIPELINE_NOTIFY_EMAIL_PORT",
    "PIPELINE_NOTIFY_EMAIL_FROM",
    "PIPELINE_NOTIFY_EMAIL_TO",
    "PIPELINE_NOTIFY_EMAIL_USER",
    "PIPELINE_NOTIFY_EMAIL_PASSWORD",
    "PIPELINE_NOTIFY_EMAIL_TIMEOUT",
)

#: The Plane keys prompted once ticketing is enabled.
_PLANE_PROMPTS: tuple[str, ...] = (
    "PLANE_BASE",
    "PLANE_API_KEY",
    "PLANE_WORKSPACE",
    "PLANE_PROJECT",
)

#: The service questions, in the order they are asked.
_SERVICE_PROMPTS: tuple[str, ...] = ("render", "enable", "start")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _secret_keys() -> tuple[str, ...]:
    """The schema keys whose value must never be echoed."""
    return tuple(spec.name for spec in schema.SCHEMA if spec.secret)


_SECRETS = frozenset(_secret_keys())


def _mask(key: str, value: object) -> str:
    """Render ``value`` for output, masking a secret key's value."""
    if key in _SECRETS:
        return MASK
    return "" if value is None else str(value)


def _masked(values: Mapping[str, object]) -> dict[str, str]:
    """Return ``values`` with every secret value replaced by :data:`MASK`."""
    return {key: _mask(key, value) for key, value in values.items()}


def _get(args: object, name: str, default=None):
    """Read ``name`` from a namespace or a mapping, with a default."""
    if isinstance(args, Mapping):
        return args.get(name, default)
    return getattr(args, name, default)


def _as_list(value: object) -> list:
    """Coerce a CLI list-ish argument to a list without failing on ``None``."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    try:
        return list(value)
    except TypeError:
        return []


def _as_set(value: object) -> set[str]:
    """Coerce a CLI set-ish argument to a set of section names."""
    return {str(item) for item in _as_list(value)}


def _normalize(key: str, raw: object) -> tuple[bool, str]:
    """Validate ``raw`` for ``key`` through the schema, never raising."""
    if raw is None:
        return False, f"{key} has no value"
    try:
        text = raw if isinstance(raw, str) else str(raw)
    except Exception:  # noqa: BLE001 - a hostile __str__ must not escape
        return False, f"{key} could not be read as text"
    return schema.validate_value(key, text)


def _error_for(key: str, message: str) -> str:
    """Prefix ``message`` with ``key``, never echoing a secret value."""
    if key in _SECRETS:
        return f"{key}: {message}"
    return f"{key}: {message}"


def _registry_data(state: Mapping[str, object]) -> dict:
    """The parsed registry mapping carried by ``state``."""
    registry = state.get("registry") if isinstance(state, Mapping) else None
    if not isinstance(registry, Mapping):
        return {}
    data = registry.get("data")
    return dict(data) if isinstance(data, Mapping) else {}


def _registry_roles(state: Mapping[str, object]) -> dict:
    """The roles block the registry already declares."""
    registry = state.get("registry") if isinstance(state, Mapping) else None
    if not isinstance(registry, Mapping):
        return {}
    roles = registry.get("roles")
    return dict(roles) if isinstance(roles, Mapping) else {}


def _existing_env(state: Mapping[str, object]) -> dict[str, str]:
    """The parsed existing ``.pipeline.env`` mapping carried by ``state``."""
    env = state.get("env") if isinstance(state, Mapping) else None
    if not isinstance(env, Mapping):
        return {}
    return {str(key): "" if value is None else str(value) for key, value in env.items()}


def _declares_claude_sonnet(data: Mapping[str, object]) -> bool:
    """True when the registry declares the ``claude``/``sonnet`` pair."""
    return bool(_preset_choices(DEFAULT_PRESET, data))


def _preset_choices(preset: str, data: Mapping[str, object]) -> list[dict]:
    """Expand ``preset`` into routing choices, or ``[]`` when undeclared."""
    try:
        return providers.preset_choices(preset, dict(data))
    except ValueError:
        return []


def _section_entry(name: str, enabled: bool, reason: str | None) -> dict:
    """One entry of ``plan["sections"]``."""
    return {"name": name, "enabled": enabled, "reason": reason}


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------


def build_plan(
    args,
    *,
    state: Mapping[str, object],
    schema_module=schema,
    interactive: bool | None = None,
    ask: Callable[[str, dict], dict] | None = None,
) -> dict:
    """Build the guided-setup plan from ``args`` and the detected ``state``.

    ``args`` is a namespace (or mapping) with ``yes``, ``dry_run``, ``set``,
    ``skip``, ``repo_root``, ``data_dir``, ``port``, ``autonomy``, ``role``,
    ``provider`` and ``model``. ``state`` carries the detected state, the
    parsed existing env file under ``state["env"]``, and the parsed registry
    under ``state["registry"]["data"]``/``["roles"]``.

    Performs no filesystem I/O and prints nothing. In interactive mode the
    only input consumed is ``ask(section, current) -> dict``, called once per
    enabled section; ``None`` falls back to a private adapter over
    :mod:`pipeline.setup.ui`.

    Returns the plan dict documented in the module docstring. Bad input is
    reported in ``plan["errors"]``; nothing is raised.
    """
    del schema_module  # kept for signature compatibility; schema is imported
    errors: list[str] = []
    existing = _existing_env(state)
    fresh = not existing

    if interactive is None:
        interactive = (not bool(_get(args, "yes", False))) and ui.is_interactive()
    interactive = bool(interactive)

    flags = _build_flags(args, errors)
    skip = _resolve_skip(args, errors)
    changes: dict[str, str] = {}
    preserve: dict[str, str] = dict(existing)
    routing = {"preset": None, "choices": []}
    next_steps: list[str] = []

    enabled = {name: name not in skip for name in SECTION_ORDER}
    reasons: dict[str, str | None] = {
        name: (f"skipped with --skip-{name}" if name in skip else None)
        for name in SECTION_ORDER
    }
    if not interactive and "services" not in skip:
        # A non-interactive install never starts a daemon behind the operator.
        enabled["services"] = False
        reasons["services"] = (
            "services are not started by a non-interactive install; re-run "
            "interactively or run the start scripts"
        )

    # -- explicit --set values win over everything else ---------------------
    explicit = _parse_set(_as_list(_get(args, "set")), errors)
    for key, value in explicit.items():
        changes[key] = value
        preserve.pop(key, None)

    # -- fresh-install defaults --------------------------------------------
    if fresh:
        _apply_fresh_defaults(changes, errors)

    # -- flags that write keys ---------------------------------------------
    _apply_flag_changes(args, flags, changes, preserve, errors)

    # -- routing -----------------------------------------------------------
    _plan_routing(
        args,
        state=state,
        routing=routing,
        changes=changes,
        errors=errors,
        enabled=enabled,
        reasons=reasons,
        next_steps=next_steps,
        fresh=fresh,
        interactive=interactive,
        ask=ask,
    )

    # -- interactive answers ------------------------------------------------
    if interactive:
        _collect_interactive(
            ask=ask,
            state=state,
            enabled=enabled,
            changes=changes,
            preserve=preserve,
            routing=routing,
            errors=errors,
            flags=flags,
            next_steps=next_steps,
        )

    # -- a fresh install with no repo root names the next step -------------
    if fresh and "REPO_ROOT" not in changes:
        next_steps.append(
            "Set the managed repository with --repo-root <path> (or pick it in "
            "the dashboard workspace picker)."
        )

    sections = [
        _section_entry(name, enabled[name], reasons[name]) for name in SECTION_ORDER
    ]

    return {
        "interactive": interactive,
        "dry_run": bool(_get(args, "dry_run", False)),
        "errors": errors,
        "sections": sections,
        "changes": changes,
        "preserve": preserve,
        "routing": routing,
        "flags": flags,
        "next_steps": next_steps,
    }


def _build_flags(args, errors: list[str]) -> dict:
    """Validate and normalize the scalar CLI flags."""
    port = _get(args, "port", None)
    if port is None:
        port = DEFAULT_PORT
    else:
        ok, normalized = _normalize_port(port)
        if not ok:
            errors.append(normalized)
            port = DEFAULT_PORT
        else:
            port = normalized

    data_dir = _get(args, "data_dir", None)
    resolved_data_dir = None
    if data_dir is not None:
        ok, normalized = _normalize_data_dir(data_dir)
        if not ok:
            errors.append(normalized)
        else:
            resolved_data_dir = normalized

    repo_root = _get(args, "repo_root", None)
    resolved_repo_root = None
    if repo_root is not None:
        ok, normalized = _normalize_repo_root(repo_root)
        if not ok:
            errors.append(normalized)
        else:
            resolved_repo_root = normalized

    return {
        "repo_root": resolved_repo_root,
        "data_dir": resolved_data_dir,
        "port": port,
        "role": _get(args, "role", None),
        "provider": _get(args, "provider", None),
        "model": _get(args, "model", None),
        "autonomy": _get(args, "autonomy", None),
    }


def _normalize_port(port: object) -> tuple[bool, object]:
    """Return ``(True, int)`` for a valid port, else ``(False, message)``."""
    if isinstance(port, bool):
        return False, f"port must be a whole number between 1 and 65535"
    try:
        value = int(port)
    except (TypeError, ValueError):
        return False, "port must be a whole number between 1 and 65535"
    if not 1 <= value <= 65535:
        return False, f"port must be between 1 and 65535, got {value}"
    return True, value


def _normalize_data_dir(data_dir: object) -> tuple[bool, str | None]:
    """Expand and require an absolute ``--data-dir``."""
    try:
        text = data_dir if isinstance(data_dir, str) else str(data_dir)
    except Exception:  # noqa: BLE001 - a hostile __str__ must not escape
        return False, "--data-dir could not be read as text"
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        return False, "--data-dir must be an absolute path (or start with ~)"
    return True, expanded


def _normalize_repo_root(repo_root: object) -> tuple[bool, str | None]:
    """Expand and require an absolute ``--repo-root``."""
    try:
        text = repo_root if isinstance(repo_root, str) else str(repo_root)
    except Exception:  # noqa: BLE001 - a hostile __str__ must not escape
        return False, "--repo-root could not be read as text"
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        return False, "--repo-root must be an absolute path (or start with ~)"
    return True, expanded


def _resolve_skip(args, errors: list[str]) -> set[str]:
    """Validate the ``--skip-<section>`` set, reporting unknown names."""
    skip = _as_set(_get(args, "skip", None))
    for name in sorted(skip):
        if name not in SECTION_ORDER:
            errors.append(
                f"unknown setup section {name!r}; valid sections are "
                + ", ".join(SECTION_ORDER)
            )
    return {name for name in skip if name in SECTION_ORDER}


def _parse_set(tokens: Sequence[str], errors: list[str]) -> dict[str, str]:
    """Parse ``KEY=VALUE`` tokens into validated, normalized changes."""
    parsed: dict[str, str] = {}
    for token in tokens:
        text = token if isinstance(token, str) else str(token)
        if "=" not in text:
            errors.append(
                f"{text}: expected KEY=VALUE (the '=' is missing)"
                if text not in _SECRETS
                else f"{text}: expected KEY=VALUE (the '=' is missing)"
            )
            continue
        key, _, raw = text.partition("=")
        key = key.strip()
        ok, normalized = _normalize(key, raw)
        if not ok:
            errors.append(_error_for(key, normalized))
            continue
        parsed[key] = normalized
    return parsed


def _apply_fresh_defaults(changes: dict[str, str], errors: list[str]) -> None:
    """Contribute the schema defaults for a fresh, non-interactive install."""
    for spec in schema.SCHEMA:
        if spec.name in _NEVER_DEFAULTED:
            continue
        if spec.name in changes:
            continue
        if spec.default is None:
            continue
        ok, normalized = _normalize(spec.name, spec.default)
        if not ok:
            errors.append(_error_for(spec.name, normalized))
            continue
        changes[spec.name] = normalized


def _apply_flag_changes(
    args,
    flags: Mapping[str, object],
    changes: dict[str, str],
    preserve: dict[str, str],
    errors: list[str],
) -> None:
    """Turn ``--data-dir``, ``--repo-root`` and ``--autonomy`` into changes."""
    data_dir = flags.get("data_dir")
    if data_dir:
        for key, value in (
            ("PLAN_DIR", str(Path(str(data_dir)) / "plans")),
            ("WORKTREE_ROOT", str(Path(str(data_dir)) / "worktrees")),
        ):
            ok, normalized = _normalize(key, value)
            if not ok:
                errors.append(_error_for(key, normalized))
                continue
            changes[key] = normalized
            preserve.pop(key, None)

    repo_root = flags.get("repo_root")
    if repo_root:
        ok, normalized = _normalize("REPO_ROOT", str(repo_root))
        if not ok:
            errors.append(_error_for("REPO_ROOT", normalized))
        else:
            changes["REPO_ROOT"] = normalized
            preserve.pop("REPO_ROOT", None)

    autonomy = flags.get("autonomy")
    if autonomy is not None:
        ok, normalized = _normalize("PIPELINE_AUTONOMY", autonomy)
        if not ok:
            errors.append(_error_for("PIPELINE_AUTONOMY", normalized))
        else:
            changes["PIPELINE_AUTONOMY"] = normalized
            preserve.pop("PIPELINE_AUTONOMY", None)


def _plan_routing(
    args,
    *,
    state: Mapping[str, object],
    routing: dict,
    changes: dict[str, str],
    errors: list[str],
    enabled: dict[str, bool],
    reasons: dict[str, str | None],
    next_steps: list[str],
    fresh: bool,
    interactive: bool,
    ask,
) -> None:
    """Decide the routing preset and the explicit per-role choices."""
    role = _get(args, "role", None)
    provider = _get(args, "provider", None)
    model = _get(args, "model", None)
    data = _registry_data(state)

    if role or provider or model:
        _plan_explicit_choice(
            role=role,
            provider=provider,
            model=model,
            routing=routing,
            errors=errors,
        )
        return

    if not fresh:
        return

    if _registry_roles(state):
        # The registry already declares this operator's roles; keep them.
        enabled["routing"] = False
        reasons["routing"] = "the model registry already declares roles"
        return

    if not _declares_claude_sonnet(data):
        enabled["routing"] = False
        reasons["routing"] = (
            f"the model registry does not declare {_PRESET_PROVIDER}/"
            f"{_PRESET_MODEL}, so no preset was applied"
        )
        next_steps.append(
            "Choose routing with --role <role> --provider <provider> "
            "--model <model>."
        )
        return

    routing["preset"] = DEFAULT_PRESET
    routing["choices"] = _preset_choices(DEFAULT_PRESET, data)


def _plan_explicit_choice(
    *,
    role,
    provider,
    model,
    routing: dict,
    errors: list[str],
) -> None:
    """Validate ``--role/--provider/--model`` and record exactly one choice."""
    from pipeline.config_provenance import PIPELINE_ROLES

    if not role:
        errors.append("--provider/--model require --role")
        return
    if not provider or not model:
        errors.append("--role requires both --provider and --model")
        return
    if role not in PIPELINE_ROLES:
        errors.append(
            f"unknown role {role!r}; valid roles are " + ", ".join(PIPELINE_ROLES)
        )
        return
    routing["choices"] = [
        {"role": role, "provider": provider, "model": model}
    ]


# ---------------------------------------------------------------------------
# Interactive collection
# ---------------------------------------------------------------------------


def _collect_interactive(
    *,
    ask,
    state: Mapping[str, object],
    enabled: dict[str, bool],
    changes: dict[str, str],
    preserve: dict[str, str],
    routing: dict,
    errors: list[str],
    flags: dict,
    next_steps: list[str],
) -> None:
    """Ask once per enabled section and fold the answers into the plan."""
    adapter = ask if ask is not None else _default_ask(state=state, flags=flags)
    data = _registry_data(state)

    for name in SECTION_ORDER:
        if not enabled[name]:
            continue
        current = _current_for(name, changes=changes, preserve=preserve, flags=flags)
        try:
            answers = adapter(name, current)
        except Exception as exc:  # noqa: BLE001 - a bad prompt must not crash
            errors.append(f"{name}: could not collect answers ({type(exc).__name__})")
            continue
        if not isinstance(answers, Mapping):
            continue
        _fold_answers(
            name,
            answers,
            changes=changes,
            preserve=preserve,
            routing=routing,
            errors=errors,
            flags=flags,
            data=data,
            enabled=enabled,
            next_steps=next_steps,
        )


def _current_for(
    name: str,
    *,
    changes: Mapping[str, str],
    preserve: Mapping[str, str],
    flags: Mapping[str, object],
) -> dict:
    """The current value of every key the section's prompts touch."""
    current: dict[str, str] = {}
    for key in _prompt_keys(name):
        if key in changes:
            current[key] = changes[key]
        elif key in preserve:
            current[key] = preserve[key]
        else:
            default = schema.default_for(key) if key in _schema_keys() else None
            if default is not None:
                current[key] = default
    if name == "core":
        current["port"] = str(flags.get("port", DEFAULT_PORT))
    return current


def _schema_keys() -> frozenset[str]:
    """Every key the schema knows."""
    return frozenset(spec.name for spec in schema.SCHEMA)


def _prompt_keys(name: str) -> tuple[str, ...]:
    """The schema keys a section's curated prompts cover."""
    if name == "core":
        return _CORE_PROMPTS
    if name == "routing":
        return _ROUTING_PROMPTS
    if name == "policy":
        return _POLICY_PROMPTS + _ADVANCED_POLICY_KEYS
    if name == "notifications":
        return _NOTIFY_PROMPTS + _EMAIL_PROMPTS
    if name == "integrations":
        return _INTEGRATION_PROMPTS + _PLANE_PROMPTS
    return ()


def _fold_answers(
    name: str,
    answers: Mapping[str, object],
    *,
    changes: dict[str, str],
    preserve: dict[str, str],
    routing: dict,
    errors: list[str],
    flags: dict,
    data: Mapping[str, object],
    enabled: dict[str, bool],
    next_steps: list[str],
) -> None:
    """Validate a section's answers and record them as changes."""
    if name == "routing":
        _fold_routing(answers, routing=routing, data=data, errors=errors)
        return
    if name == "services":
        _fold_services(answers, enabled=enabled, flags=flags, errors=errors)
        return
    if name == "claude_assets":
        _fold_claude_assets(answers, enabled=enabled)
        return

    for key, raw in answers.items():
        if key == "port":
            ok, normalized = _normalize_port(raw)
            if not ok:
                errors.append(normalized)
            else:
                flags["port"] = normalized
            continue
        if key not in _schema_keys():
            continue
        if raw is None:
            continue
        ok, normalized = _normalize(key, raw)
        if not ok:
            errors.append(_error_for(key, normalized))
            continue
        changes[key] = normalized
        preserve.pop(key, None)


def _fold_routing(
    answers: Mapping[str, object],
    *,
    routing: dict,
    data: Mapping[str, object],
    errors: list[str],
) -> None:
    """Turn a routing answer into a preset expansion or one custom choice."""
    preset = answers.get("preset")
    if preset is None:
        return
    preset = str(preset)
    if preset == "custom":
        role = answers.get("role")
        provider = answers.get("provider")
        model = answers.get("model")
        _plan_explicit_choice(
            role=role, provider=provider, model=model, routing=routing, errors=errors
        )
        return
    choices = _preset_choices(preset, data)
    if not choices:
        errors.append(
            f"routing preset {preset!r} is not declared in the model registry"
        )
        return
    routing["preset"] = preset
    routing["choices"] = choices


def _fold_services(
    answers: Mapping[str, object],
    *,
    enabled: dict[str, bool],
    flags: dict,
    errors: list[str],
) -> None:
    """Turn the service answers into the section's enabled flag."""
    render = answers.get("render")
    start = answers.get("start")
    enable = answers.get("enable")
    wanted = bool(render) or bool(start) or bool(enable)
    enabled["services"] = wanted
    if not wanted:
        return
    port = answers.get("port")
    if port is not None:
        ok, normalized = _normalize_port(port)
        if not ok:
            errors.append(normalized)
        else:
            flags["port"] = normalized


def _fold_claude_assets(answers: Mapping[str, object], *, enabled: dict[str, bool]) -> None:
    """Turn the install/keep confirmation into the section's enabled flag."""
    install = answers.get("install")
    if install is None:
        return
    enabled["claude_assets"] = bool(install)


def _default_ask(*, state: Mapping[str, object], flags: dict):
    """Build the private adapter over :mod:`pipeline.setup.ui`."""

    def ask(name: str, current: Mapping[str, str]) -> dict:
        if name == "core":
            return _ask_core(current, flags=flags)
        if name == "routing":
            return _ask_routing(current, state=state)
        if name == "policy":
            return _ask_policy(current)
        if name == "notifications":
            return _ask_notifications(current)
        if name == "integrations":
            return _ask_integrations(current)
        if name == "claude_assets":
            return {"install": ui.ask_yes_no("Install the Claude Code assets?", default=True)}
        if name == "services":
            return _ask_services(current)
        return {}

    return ask


def _ask_core(current: Mapping[str, str], *, flags: dict) -> dict:
    """Ask the curated core questions."""
    answers: dict[str, str] = {}
    answers["PLAN_DIR"] = ui.prompt_value(
        "Directory holding plan files",
        kind="text",
        default=current.get("PLAN_DIR") or schema.default_for("PLAN_DIR"),
        validate=lambda raw: schema.validate_value("PLAN_DIR", raw),
    )
    if _offers_repo_root():
        target = ui.prompt_value(
            "Target project directory (blank leaves REPO_ROOT unset)",
            kind="text",
            default="",
            validate=lambda raw: schema.validate_value("REPO_ROOT", raw)
            if raw
            else (True, ""),
        )
        if target:
            answers["REPO_ROOT"] = target
    answers["port"] = ui.prompt_value(
        "Dashboard port",
        kind="text",
        default=str(flags.get("port", DEFAULT_PORT)),
        validate=lambda raw: schema.validate_value("PIPELINE_NOTIFY_EMAIL_PORT", raw),
    )
    return answers


def _offers_repo_root() -> bool:
    """True when the current directory is a git repo that is not this checkout."""
    try:
        here = Path.cwd().resolve()
    except OSError:
        return False
    if not (here / ".git").exists():
        return False
    try:
        checkout = Path(__file__).resolve().parents[2]
    except (OSError, IndexError):
        return True
    return here != checkout


def _ask_routing(current: Mapping[str, str], *, state: Mapping[str, object]) -> dict:
    """Ask the preset, and for ``custom`` one role plus a declared pair."""
    preset = ui.prompt_value(
        "Routing preset",
        kind="choice",
        default=current.get("preset") or DEFAULT_PRESET,
        choices=providers.PRESETS,
    )
    if preset != "custom":
        return {"preset": preset}
    from pipeline.config_provenance import PIPELINE_ROLES

    role = ui.prompt_value(
        "Role to route", kind="choice", default=PIPELINE_ROLES[0], choices=PIPELINE_ROLES
    )
    pairs = _declared_pairs(_registry_data(state))
    if not pairs:
        return {"preset": "custom", "role": role}
    labels = [f"{provider}/{model}" for provider, model in pairs]
    chosen = ui.prompt_value(
        "Provider/model", kind="choice", default=labels[0], choices=labels
    )
    provider, _, model = str(chosen).partition("/")
    return {"preset": "custom", "role": role, "provider": provider, "model": model}


def _declared_pairs(data: Mapping[str, object]) -> list[tuple[str, str]]:
    """Every declared ``(provider, model)`` pair in the registry."""
    pairs: list[tuple[str, str]] = []
    declared = data.get("providers") if isinstance(data, Mapping) else None
    if not isinstance(declared, Mapping):
        return pairs
    for provider, spec in declared.items():
        models = spec.get("models") if isinstance(spec, Mapping) else None
        if not isinstance(models, Mapping):
            continue
        for model in models:
            pairs.append((str(provider), str(model)))
    return pairs


def _ask_policy(current: Mapping[str, str]) -> dict:
    """Ask the policy questions, with an optional advanced-gates branch."""
    answers: dict[str, str] = {}
    for key in _POLICY_PROMPTS:
        answers[key] = ui.prompt_value(
            _prompt_label(key),
            kind="choice" if key != "PIPELINE_MAX_CONCURRENT_AGENTS" else "text",
            default=current.get(key) or schema.default_for(key),
            choices=schema._BY_NAME[key].choices,
            validate=lambda raw, k=key: schema.validate_value(k, raw),
        )
    if not ui.ask_yes_no("Customize the advanced gates?", default=False):
        return answers
    for key in _ADVANCED_POLICY_KEYS:
        default = current.get(key) or schema.default_for(key) or ""
        answers[key] = ui.prompt_value(
            _prompt_label(key),
            kind="text",
            default=default,
            validate=lambda raw, k=key: schema.validate_value(k, raw),
        )
    return answers


def _ask_notifications(current: Mapping[str, str]) -> dict:
    """Ask the outbox gate, then the email gate and its settings."""
    answers: dict[str, str] = {}
    outbox = ui.ask_yes_no(
        "Write pipeline events to the outbox?",
        default=current.get("PIPELINE_NOTIFY_OUTBOX_ENABLED") == "1",
    )
    answers["PIPELINE_NOTIFY_OUTBOX_ENABLED"] = "1" if outbox else "0"
    if outbox:
        answers["PIPELINE_NOTIFY_OUTBOX_EVENTS"] = ui.prompt_value(
            _prompt_label("PIPELINE_NOTIFY_OUTBOX_EVENTS"),
            kind="text",
            default=current.get("PIPELINE_NOTIFY_OUTBOX_EVENTS")
            or schema.default_for("PIPELINE_NOTIFY_OUTBOX_EVENTS"),
            validate=lambda raw: schema.validate_value("PIPELINE_NOTIFY_OUTBOX_EVENTS", raw),
        )
    email = ui.ask_yes_no(
        "Send notification emails?",
        default=current.get("PIPELINE_NOTIFY_EMAIL_ENABLED") == "1",
    )
    answers["PIPELINE_NOTIFY_EMAIL_ENABLED"] = "1" if email else "0"
    if not email:
        return answers
    for key in _EMAIL_PROMPTS:
        answers[key] = ui.prompt_value(
            _prompt_label(key),
            kind="secret" if key in _SECRETS else "text",
            default=current.get(key) or schema.default_for(key) or "",
            validate=lambda raw, k=key: schema.validate_value(k, raw),
        )
    return answers


def _ask_integrations(current: Mapping[str, str]) -> dict:
    """Ask the Plane opt-in, then its settings and the ticket provider."""
    answers: dict[str, str] = {}
    plane = ui.ask_yes_no("Enable the Plane integration?", default=False)
    if plane:
        for key in _PLANE_PROMPTS:
            answers[key] = ui.prompt_value(
                _prompt_label(key),
                kind="secret" if key in _SECRETS else "text",
                default=current.get(key) or schema.default_for(key) or "",
                validate=lambda raw, k=key: schema.validate_value(k, raw),
            )
        answers["PIPELINE_TICKET_PROVIDER"] = "plane"
        return answers
    answers["PIPELINE_TICKET_PROVIDER"] = ui.prompt_value(
        _prompt_label("PIPELINE_TICKET_PROVIDER"),
        kind="choice",
        default=current.get("PIPELINE_TICKET_PROVIDER")
        or schema.default_for("PIPELINE_TICKET_PROVIDER"),
        choices=schema._BY_NAME["PIPELINE_TICKET_PROVIDER"].choices,
    )
    return answers


def _ask_services(current: Mapping[str, str]) -> dict:
    """Ask the three service questions; only "start now" defaults to yes."""
    return {
        "render": ui.ask_yes_no("Render the service definitions?", default=False),
        "enable": ui.ask_yes_no("Enable the scheduler at login?", default=False),
        "start": ui.ask_yes_no("Start the dashboard and scheduler now?", default=True),
        "port": current.get("port", str(DEFAULT_PORT)),
    }


def _prompt_label(key: str) -> str:
    """The schema's prompt text for ``key``, falling back to the key itself."""
    spec = schema._BY_NAME.get(key)
    return spec.prompt if spec is not None and spec.prompt else key


# ---------------------------------------------------------------------------
# run_plan
# ---------------------------------------------------------------------------


def run_plan(
    plan: Mapping[str, object],
    *,
    output: Callable[[str], None] = print,
    env_writer: Callable[..., dict] | None = None,
    provider_applier: Callable[..., object] | None = None,
    asset_installer: Callable[..., object] | None = None,
    service_runner: Callable[..., object] | None = None,
    state: Mapping[str, object] | None = None,
) -> dict:
    """Apply ``plan``, or describe it when it is a dry run.

    A plan carrying errors is returned untouched and no writer is called. A
    dry run prints one ``KEY=value`` line per planned change (secrets masked)
    plus one line per section, calls no injected applier, and writes nothing.

    Otherwise the env file is written once with ``preserve`` and ``changes``
    merged, the plan/worktree directories are created when the core section
    changed, and then routing, the Claude assets, and the services are
    applied in that order — env first, so a started process sources the new
    file. A failure in any step is recorded in ``errors`` and the remaining
    steps still run.
    """
    errors = list(plan.get("errors") or ())
    if errors:
        return {"applied": False, "errors": errors}

    changes = dict(plan.get("changes") or {})
    preserve = dict(plan.get("preserve") or {})
    sections = list(plan.get("sections") or ())
    routing = dict(plan.get("routing") or {})
    flags = dict(plan.get("flags") or {})
    next_steps = list(plan.get("next_steps") or ())

    if plan.get("dry_run"):
        return _dry_run(
            changes=changes,
            sections=sections,
            output=output,
            next_steps=next_steps,
        )

    result: dict[str, object] = {
        "applied": True,
        "changed": _masked(changes),
        "env": None,
        "registry": None,
        "assets": None,
        "services": None,
        "skipped": [],
        "errors": [],
        "next_steps": next_steps,
    }
    skipped: list[str] = result["skipped"]  # type: ignore[assignment]
    failures: list[str] = result["errors"]  # type: ignore[assignment]

    enabled = {entry.get("name"): bool(entry.get("enabled")) for entry in sections}

    # (a) the env file, once.
    merged = {**preserve, **changes}
    writer = env_writer if env_writer is not None else envfile.write_env_file
    try:
        result["env"] = writer(
            _env_path(flags, state),
            merged,
            secret_keys=_secret_keys(),
        )
    except Exception as exc:  # noqa: BLE001 - a host failure is data
        failures.append(f"writing {ENV_FILE_NAME} failed: {exc}")

    # (b) the directories, only when core changed.
    if enabled.get("core"):
        _create_directories(changes, failures)

    # (c) routing.
    choices = list(routing.get("choices") or ())
    if choices:
        applier = provider_applier if provider_applier is not None else _default_applier
        try:
            result["registry"] = applier(choices=choices, flags=flags, state=state)
        except Exception as exc:  # noqa: BLE001 - a host failure is data
            failures.append(f"applying routing failed: {exc}")
    else:
        skipped.append("routing")

    # (d) the Claude assets and the MCP registration.
    if enabled.get("claude_assets"):
        installer = (
            asset_installer if asset_installer is not None else _default_asset_installer
        )
        try:
            result["assets"] = installer(flags=flags, state=state)
        except Exception as exc:  # noqa: BLE001 - a host failure is data
            failures.append(f"installing the Claude assets failed: {exc}")
    else:
        skipped.append("claude_assets")

    # (e) the services, last so they source the new env file.
    if enabled.get("services"):
        runner = service_runner if service_runner is not None else _default_service_runner
        try:
            result["services"] = runner(flags=flags, state=state)
        except Exception as exc:  # noqa: BLE001 - a host failure is data
            failures.append(f"starting the services failed: {exc}")
        else:
            _record_service_health(result, flags, failures)
    else:
        skipped.append("services")

    for entry in sections:
        if not entry.get("enabled") and entry.get("name") not in skipped:
            skipped.append(entry.get("name"))

    _record_asset_failures(result, failures)
    return result


def _env_path(flags: Mapping[str, object], state: Mapping[str, object] | None) -> Path:
    """The ``.pipeline.env`` path the plan writes."""
    repo_root = flags.get("repo_root")
    if not repo_root and state is not None:
        repo_root = _existing_env(state).get("REPO_ROOT")
    if not repo_root:
        repo_root = str(Path.cwd())
    return Path(str(repo_root)) / ENV_FILE_NAME


def _dry_run(
    *,
    changes: Mapping[str, str],
    sections: Sequence[Mapping[str, object]],
    output: Callable[[str], None],
    next_steps: Sequence[str],
) -> dict:
    """Print the plan and return the dry-run result without touching anything."""
    for key, value in changes.items():
        output(f"{key}={_mask(key, value)}")
    for entry in sections:
        name = entry.get("name")
        if entry.get("enabled"):
            output(f"section {name}: enabled")
        else:
            reason = entry.get("reason") or "disabled"
            output(f"section {name}: skipped ({reason})")
    return {
        "applied": False,
        "dry_run": True,
        "planned": _masked(changes),
        "sections": [dict(entry) for entry in sections],
        "next_steps": list(next_steps),
        "errors": [],
    }


def _create_directories(changes: Mapping[str, str], failures: list[str]) -> None:
    """Create ``PLAN_DIR`` and ``WORKTREE_ROOT`` with mode 0755."""
    for key in ("PLAN_DIR", "WORKTREE_ROOT"):
        raw = changes.get(key)
        if not raw:
            continue
        try:
            Path(raw).mkdir(parents=True, exist_ok=True, mode=0o755)
        except OSError as exc:
            failures.append(f"creating {key} failed: {exc}")


def _default_applier(*, choices, flags, state):
    """The real routing applier: ensure the local registry, then apply."""
    repo_root = flags.get("repo_root") or _state_repo_root(state) or str(Path.cwd())
    registry_path = providers.ensure_local_registry(repo_root)
    return providers.apply_choices(choices, registry_path=registry_path)


def _state_repo_root(state: Mapping[str, object] | None) -> str | None:
    """The repo root the detected state carries, if any."""
    if state is None:
        return None
    env = _existing_env(state)
    return env.get("REPO_ROOT") or None


def _default_asset_installer(*, flags, state):
    """The real asset installer: personas/policy, then the MCP registration."""
    repo_root = flags.get("repo_root") or _state_repo_root(state) or str(Path.cwd())
    assets = claude_setup.install_claude_assets(repo_root=repo_root)
    mcp = claude_setup.register_mcp_server(repo_root=repo_root)
    return {"assets": assets, "mcp": mcp}


def _default_service_runner(*, flags, state):
    """The real service runner: render the definitions, then start them."""
    repo_root = flags.get("repo_root") or _state_repo_root(state) or str(Path.cwd())
    render = services.render_services(
        repo_root=repo_root,
        platform=None,
        mlx_model_path=None,
        enable=False,
    )
    start = services.start_services(
        repo_root=repo_root, port=int(flags.get("port") or DEFAULT_PORT)
    )
    return {"render": render, "start": start}


def _record_service_health(
    result: dict, flags: Mapping[str, object], failures: list[str]
) -> None:
    """Verify the dashboard answers, using the shared secret lazily."""
    started = result.get("services")
    if not isinstance(started, Mapping):
        return
    start = started.get("start")
    if not isinstance(start, Mapping):
        return
    dashboard = start.get("dashboard")
    if not isinstance(dashboard, Mapping) or not dashboard.get("ok"):
        return
    try:
        health = _verify_health(int(flags.get("port") or DEFAULT_PORT))
    except Exception as exc:  # noqa: BLE001 - a host failure is data
        failures.append(f"verifying the dashboard health failed: {exc}")
        return
    result["health"] = health
    if not health.get("healthy"):
        failures.append(
            "the dashboard did not answer its health check: "
            + str(health.get("reason") or "unknown reason")
        )


def _verify_health(port: int) -> dict:
    """Probe the dashboard, obtaining the shared secret lazily.

    The secret is passed to :func:`pipeline.setup.services.verify_health` and
    never stored in the returned dict.
    """
    from app.auth import get_or_create_api_key

    api_key = get_or_create_api_key()
    healthy, reason = services.verify_health(port=port, api_key=api_key)
    return {"healthy": bool(healthy), "reason": reason}


def _record_asset_failures(result: dict, failures: list[str]) -> None:
    """Surface a failed MCP registration and a failed render as errors."""
    assets = result.get("assets")
    if isinstance(assets, Mapping):
        mcp = assets.get("mcp")
        if isinstance(mcp, Mapping) and mcp.get("status") == "failed":
            failures.append(
                "registering the MCP server failed: "
                + str(mcp.get("error") or "unknown error")
            )
    started = result.get("services")
    if isinstance(started, Mapping):
        render = started.get("render")
        if isinstance(render, Mapping) and render.get("status") == "failed":
            failures.append(
                "rendering the service definitions failed: "
                + str(render.get("error") or "unknown error")
            )


# ---------------------------------------------------------------------------
# render_summary
# ---------------------------------------------------------------------------


def render_summary(result: Mapping[str, object]) -> str:
    """Render ``result`` as deterministic, secret-free text.

    One line per section, then the masked changes, then any errors, then the
    next steps. A step the result marks failed or skipped is never claimed to
    have succeeded, and a secret value is never rendered.
    """
    lines: list[str] = ["Guided setup summary", ""]

    sections = list(result.get("sections") or ())
    if sections:
        lines.append("Sections:")
        for entry in sections:
            name = entry.get("name")
            if entry.get("enabled"):
                lines.append(f"  {name}: enabled")
            else:
                reason = entry.get("reason") or "disabled"
                lines.append(f"  {name}: skipped ({reason})")
        lines.append("")

    changed = dict(result.get("changed") or {})
    if changed:
        lines.append("Changes:")
        for key, value in changed.items():
            lines.append(f"  {key}={_mask(key, value)}")
        lines.append("")

    errors = list(result.get("errors") or ())
    if errors:
        lines.append("Errors:")
        for error in errors:
            lines.append(f"  {error}")
        lines.append("")

    autonomy = _summary_autonomy(result)
    if autonomy == "dry-run":
        lines.append(describe_autonomy("dry-run"))
        lines.append("")

    lines.append("Next steps:")
    for step in _summary_next_steps(result):
        lines.append(f"  - {step}")
    return "\n".join(lines).rstrip() + "\n"


def _summary_autonomy(result: Mapping[str, object]) -> str | None:
    """The autonomy mode the result's changes record, if any."""
    changed = result.get("changed")
    if isinstance(changed, Mapping):
        value = changed.get("PIPELINE_AUTONOMY")
        if isinstance(value, str):
            return value
    return None


def _summary_next_steps(result: Mapping[str, object]) -> list[str]:
    """The ordered next steps for ``result``, never claiming a failed step."""
    steps: list[str] = []
    for step in result.get("next_steps") or ():
        steps.append(str(step))

    if _mcp_skipped(result):
        manual = _mcp_manual(result) or MCP_MANUAL_COMMAND
        steps.append(f"Register the MCP server by hand: {manual}")

    steps.append(RESTART_STEP)
    steps.append(TEST_STEP)
    return steps


def _mcp_skipped(result: Mapping[str, object]) -> bool:
    """True when the MCP registration did not succeed."""
    assets = result.get("assets")
    if not isinstance(assets, Mapping):
        return False
    mcp = assets.get("mcp")
    if not isinstance(mcp, Mapping):
        return False
    return mcp.get("status") in ("failed", "skipped")


def _mcp_manual(result: Mapping[str, object]) -> str | None:
    """The manual MCP command the registration result carries, if any."""
    assets = result.get("assets")
    if not isinstance(assets, Mapping):
        return None
    mcp = assets.get("mcp")
    if not isinstance(mcp, Mapping):
        return None
    manual = mcp.get("manual")
    return str(manual) if manual else None
