"""Orchestrate the guided setup into an idempotent, dry-runnable plan.

``build_plan`` turns the CLI arguments plus the detected machine state into a
plan: which sections are enabled, which ``.pipeline.env`` keys change, which
existing values are preserved, and which routing choices to apply. It performs
no filesystem I/O and prints nothing; in interactive mode the only input it
consumes is the injected ``ask`` callable.

``run_plan`` is the single writer: it merges the preserved values with the
planned changes, writes ``.pipeline.env`` once, creates the plan/worktree
directories, then applies routing, installs the Claude assets, and
renders/starts the services. Every host effect is injectable, so a test never
touches the real machine. A failure in one step is recorded in ``errors`` and
the remaining steps still run.

``render_summary`` renders deterministic, secret-free text describing what
changed, what was skipped, what failed, and what to do next.

Design rules, all load-bearing:

* ``build_plan`` is pure: it reads ``state`` (collected by
  :mod:`pipeline.setup.detect` plus the parsed ``.pipeline.env``) and never
  opens a file, so a dry run cannot leave a trace.
* Every value reaching ``plan["changes"]`` — schema defaults included — goes
  through :func:`pipeline.setup.schema.validate_value`, and the *normalized*
  value is stored: a path is absolute, a bool is ``"1"``/``"0"``.
* A secret is never echoed. Error text may name the key; the value is never
  rendered, logged, returned, or summarised.
* Nothing is raised at the caller for a host condition. Bad input becomes an
  entry in ``plan["errors"]``; a failing applier becomes one in
  ``result["errors"]``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from . import claude_setup, envfile, providers, services, ui
from . import schema as _schema
from .policy import describe_autonomy
from .schema import SCHEMA

__all__ = ["build_plan", "render_summary", "run_plan"]

#: The fixed order sections are planned, applied, and reported in.
SECTION_ORDER: tuple[str, ...] = (
    "core", "routing", "policy", "notifications",
    "integrations", "claude_assets", "services",
)

#: The default dashboard port. Never 8000: that is the dev server's port.
DEFAULT_PORT = 8001

#: The default data directory the core prompt offers.
DEFAULT_DATA_DIR = "~/.claude"

#: The routing preset a fresh non-interactive install applies.
DEFAULT_PRESET = "claude"

#: The provider/model the ``claude`` preset needs to be declared.
_PRESET_PROVIDER, _PRESET_MODEL = "claude", "sonnet"

#: The env file the plan writes, relative to the managed repository.
ENV_FILE_NAME = ".pipeline.env"

#: Rendered in place of a secret value, everywhere.
MASK = "***"

#: The manual MCP registration command, shown when the automatic one failed.
MCP_MANUAL_COMMAND = "claude mcp add -s user pipeline <python> <repo>/server.py"

#: Named when the operator declines the advanced-gates branch.
_ADVANCED_GATES_STEP = (
    "Change the usage thresholds, pause/resume gates, escalation backend/model, "
    "or security-audit cadence later with --set KEY=VALUE."
)

#: The two next steps every summary ends with.
RESTART_STEP = "Restart Claude Code so it picks up the registered server."
TEST_STEP = "Run .venv/bin/python -m pytest -q"

#: Policy keys the "advanced gates" branch owns. Declining keeps them at their
#: schema default (or unset, for the escalation pair).
_ADVANCED_POLICY_KEYS: tuple[str, ...] = (
    "PIPELINE_DAILY_REQUEST_THRESHOLD", "PIPELINE_WEEKLY_REQUEST_THRESHOLD",
    "PIPELINE_PAUSE_THRESHOLD", "PIPELINE_RESUME_THRESHOLD",
    "PIPELINE_ESCALATION_BACKEND", "PIPELINE_ESCALATION_MODEL",
    "PIPELINE_SECURITY_AUDIT_EVERY_DAYS", "PIPELINE_SECURITY_AUDIT_EVERY_COMMITS",
)

#: Keys that stay unset on a fresh install even though the schema has a value.
_NEVER_DEFAULTED = frozenset(
    {"PIPELINE_ESCALATION_BACKEND", "PIPELINE_ESCALATION_MODEL"}
)

#: The curated interactive prompts, per section.
_CORE_PROMPTS = ("PLAN_DIR", "REPO_ROOT")
_ROUTING_PROMPTS = ("preset",)
_POLICY_PROMPTS = (
    "PIPELINE_AUTONOMY", "PIPELINE_RISK_THRESHOLD", "PIPELINE_MAX_CONCURRENT_AGENTS",
)
_NOTIFY_PROMPTS = ("PIPELINE_NOTIFY_OUTBOX_ENABLED", "PIPELINE_NOTIFY_EMAIL_ENABLED")
_INTEGRATION_PROMPTS = ("PIPELINE_TICKET_PROVIDER",)
_EMAIL_PROMPTS = (
    "PIPELINE_NOTIFY_EMAIL_HOST", "PIPELINE_NOTIFY_EMAIL_PORT",
    "PIPELINE_NOTIFY_EMAIL_FROM", "PIPELINE_NOTIFY_EMAIL_TO",
    "PIPELINE_NOTIFY_EMAIL_USER", "PIPELINE_NOTIFY_EMAIL_PASSWORD",
    "PIPELINE_NOTIFY_EMAIL_TIMEOUT",
)
_PLANE_PROMPTS = ("PLANE_BASE", "PLANE_API_KEY", "PLANE_WORKSPACE", "PLANE_PROJECT")

#: Every key the schema knows, the specs by name, and the secret keys.
_SPECS = {spec.name: spec for spec in SCHEMA}
_SCHEMA_KEYS = frozenset(_SPECS)
_SECRETS = frozenset(spec.name for spec in SCHEMA if spec.secret)


# -- small helpers ----------------------------------------------------------


def _secret_keys() -> tuple[str, ...]:
    return tuple(spec.name for spec in SCHEMA if spec.secret)


def _mask(key: str, value: object) -> str:
    if key in _SECRETS:
        return MASK
    return "" if value is None else str(value)


def _masked(values: Mapping[str, object]) -> dict[str, str]:
    return {key: _mask(key, value) for key, value in values.items()}


def _get(args: object, name: str, default=None):
    if isinstance(args, Mapping):
        return args.get(name, default)
    return getattr(args, name, default)


def _as_list(value: object) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    try:
        return list(value)
    except TypeError:
        return []


def _normalize(key: str, raw: object) -> tuple[bool, str]:
    if raw is None:
        return False, f"{key} has no value"
    try:
        text = raw if isinstance(raw, str) else str(raw)
    except Exception:  # noqa: BLE001 - a hostile __str__ must not escape
        return False, f"{key} could not be read as text"
    return _schema.validate_value(key, text)


def _registry(state: Mapping[str, object]) -> Mapping:
    block = state.get("registry") if isinstance(state, Mapping) else None
    return block if isinstance(block, Mapping) else {}


def _registry_data(state: Mapping[str, object]) -> dict:
    data = _registry(state).get("data")
    return dict(data) if isinstance(data, Mapping) else {}


def _registry_roles(state: Mapping[str, object]) -> dict:
    roles = _registry(state).get("roles")
    return dict(roles) if isinstance(roles, Mapping) else {}


def _existing_env(state: Mapping[str, object]) -> dict[str, str]:
    env = state.get("env") if isinstance(state, Mapping) else None
    if not isinstance(env, Mapping):
        return {}
    return {str(key): "" if value is None else str(value) for key, value in env.items()}


def _preset_choices(preset: str, data: Mapping[str, object]) -> list[dict]:
    try:
        return providers.preset_choices(preset, dict(data))
    except ValueError:
        return []


def _section_entry(name: str, enabled: bool, reason: str | None) -> dict:
    return {"name": name, "enabled": enabled, "reason": reason}


def _normalize_port(port: object) -> tuple[bool, object]:
    message = "port must be a whole number between 1 and 65535"
    if isinstance(port, bool):
        return False, message
    try:
        value = int(port)
    except (TypeError, ValueError):
        return False, message
    if not 1 <= value <= 65535:
        return False, f"port must be between 1 and 65535, got {value}"
    return True, value


def _normalize_path_flag(flag: str, raw: object) -> tuple[bool, str | None]:
    try:
        text = raw if isinstance(raw, str) else str(raw)
    except Exception:  # noqa: BLE001 - a hostile __str__ must not escape
        return False, f"{flag} could not be read as text"
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        return False, f"{flag} must be an absolute path (or start with ~)"
    return True, expanded


# -- build_plan -------------------------------------------------------------


def build_plan(
    args, *, state: Mapping[str, object], schema: Sequence = SCHEMA,
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
    :mod:`pipeline.setup.ui`. Bad input is reported in ``plan["errors"]``.
    """
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
    routing: dict = {"preset": None, "choices": []}
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

    # Explicit --set values win over every default.
    for key, value in _parse_set(_as_list(_get(args, "set")), errors).items():
        changes[key] = value
        preserve.pop(key, None)

    if fresh:
        _apply_fresh_defaults(schema, changes, errors)
    _apply_flag_changes(flags, changes, preserve, errors)
    _plan_routing(
        args, state=state, routing=routing, errors=errors, enabled=enabled,
        reasons=reasons, next_steps=next_steps, fresh=fresh,
    )

    if interactive:
        _collect_interactive(
            ask=ask, state=state, enabled=enabled, changes=changes,
            preserve=preserve, routing=routing, errors=errors, flags=flags,
            next_steps=next_steps,
        )

    if fresh and "REPO_ROOT" not in changes:
        next_steps.append(
            "Set the managed repository with --repo-root <path> (or pick it in "
            "the dashboard workspace picker)."
        )


    return {
        "interactive": interactive,
        "dry_run": bool(_get(args, "dry_run", False)),
        "errors": errors,
        "sections": [
            _section_entry(name, enabled[name], reasons[name]) for name in SECTION_ORDER
        ],
        "changes": changes, "preserve": preserve, "routing": routing,
        "flags": flags, "next_steps": next_steps,
    }


def _build_flags(args, errors: list[str]) -> dict:
    port = _get(args, "port", None)
    if port is None:
        port = DEFAULT_PORT
    else:
        ok, normalized = _normalize_port(port)
        port = normalized if ok else DEFAULT_PORT
        if not ok:
            errors.append(normalized)

    resolved: dict[str, object] = {"port": port}
    for name, flag in (("data_dir", "--data-dir"), ("repo_root", "--repo-root")):
        raw = _get(args, name, None)
        resolved[name] = None
        if raw is not None:
            ok, normalized = _normalize_path_flag(flag, raw)
            if not ok:
                errors.append(normalized)
            else:
                resolved[name] = normalized
    for name in ("role", "provider", "model", "autonomy"):
        resolved[name] = _get(args, name, None)
    return resolved


def _resolve_skip(args, errors: list[str]) -> set[str]:
    skip = {str(item) for item in _as_list(_get(args, "skip", None))}
    for name in sorted(skip):
        if name not in SECTION_ORDER:
            errors.append(
                f"unknown setup section {name!r}; valid sections are "
                + ", ".join(SECTION_ORDER)
            )
    return {name for name in skip if name in SECTION_ORDER}


def _parse_set(tokens: Sequence[str], errors: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for token in tokens:
        text = token if isinstance(token, str) else str(token)
        if "=" not in text:
            errors.append(f"{text}: expected KEY=VALUE (the '=' is missing)")
            continue
        key, _, raw = text.partition("=")
        key = key.strip()
        ok, normalized = _normalize(key, raw)
        if not ok:
            errors.append(f"{key}: {normalized}")
            continue
        parsed[key] = normalized
    return parsed


def _apply_fresh_defaults(specs: Sequence, changes: dict, errors: list[str]) -> None:
    for spec in specs:
        if spec.name in _NEVER_DEFAULTED or spec.name in changes:
            continue
        if spec.default is None:
            continue
        ok, normalized = _normalize(spec.name, spec.default)
        if not ok:
            errors.append(f"{spec.name}: {normalized}")
            continue
        changes[spec.name] = normalized


def _apply_flag_changes(flags: Mapping, changes: dict, preserve: dict, errors: list) -> None:
    data_dir = flags.get("data_dir")
    if data_dir:
        for key, value in (
            ("PLAN_DIR", str(Path(str(data_dir)) / "plans")),
            ("WORKTREE_ROOT", str(Path(str(data_dir)) / "worktrees")),
        ):
            ok, normalized = _normalize(key, value)
            if not ok:
                errors.append(f"{key}: {normalized}")
                continue
            changes[key] = normalized
            preserve.pop(key, None)

    for key, flag in (("REPO_ROOT", "repo_root"), ("PIPELINE_AUTONOMY", "autonomy")):
        if flags.get(flag) is None:
            continue
        ok, normalized = _normalize(key, str(flags[flag]))
        if not ok:
            errors.append(f"{key}: {normalized}")
            continue
        changes[key] = normalized
        preserve.pop(key, None)


def _plan_routing(args, *, state, routing, errors, enabled, reasons, next_steps, fresh) -> None:
    role = _get(args, "role", None)
    provider = _get(args, "provider", None)
    model = _get(args, "model", None)
    if role or provider or model:
        _plan_explicit_choice(
            role=role, provider=provider, model=model, routing=routing, errors=errors
        )
        return
    if not fresh:
        return
    if _registry_roles(state):
        # The registry already declares this operator's roles; keep them.
        enabled["routing"] = False
        reasons["routing"] = "the model registry already declares roles"
        return

    choices = _preset_choices(DEFAULT_PRESET, _registry_data(state))
    if not choices:
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
    routing["choices"] = choices


def _plan_explicit_choice(*, role, provider, model, routing, errors) -> None:
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
    routing["choices"] = [{"role": role, "provider": provider, "model": model}]


# -- interactive collection -------------------------------------------------


def _collect_interactive(*, ask, state, enabled, changes, preserve, routing, errors,
                         flags, next_steps) -> None:
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
            name, answers, changes=changes, preserve=preserve, routing=routing,
            errors=errors, flags=flags, data=data, enabled=enabled,
            next_steps=next_steps,
        )


def _prompt_keys(name: str) -> tuple[str, ...]:
    return {
        "core": _CORE_PROMPTS,
        "routing": _ROUTING_PROMPTS,
        "policy": _POLICY_PROMPTS + _ADVANCED_POLICY_KEYS,
        "notifications": _NOTIFY_PROMPTS + _EMAIL_PROMPTS,
        "integrations": _INTEGRATION_PROMPTS + _PLANE_PROMPTS,
    }.get(name, ())


def _current_for(name: str, *, changes, preserve, flags) -> dict:
    current: dict[str, str] = {}
    for key in _prompt_keys(name):
        if key in changes:
            current[key] = changes[key]
        elif key in preserve:
            current[key] = preserve[key]
        elif key in _SCHEMA_KEYS and _SPECS[key].default is not None:
            current[key] = str(_SPECS[key].default)
    if name == "core":
        current["port"] = str(flags.get("port", DEFAULT_PORT))
    return current


def _fold_answers(name, answers, *, changes, preserve, routing, errors, flags, data,
                  enabled, next_steps) -> None:
    if name == "routing":
        _fold_routing(answers, routing=routing, data=data, errors=errors)
        return
    if name == "services":
        wanted = any(bool(answers.get(key)) for key in ("render", "enable", "start"))
        enabled["services"] = wanted
        if wanted and answers.get("port") is not None:
            ok, normalized = _normalize_port(answers["port"])
            if not ok:
                errors.append(normalized)
            else:
                flags["port"] = normalized
        return
    if name == "claude_assets":
        if answers.get("install") is not None:
            enabled["claude_assets"] = bool(answers["install"])
        return
    if name == "policy" and not any(k in answers for k in _ADVANCED_POLICY_KEYS):
        next_steps.append(_ADVANCED_GATES_STEP)

    for key, raw in answers.items():
        if key == "port":
            ok, normalized = _normalize_port(raw)
            if not ok:
                errors.append(normalized)
            else:
                flags["port"] = normalized
            continue
        if key not in _SCHEMA_KEYS or raw is None:
            continue
        ok, normalized = _normalize(key, raw)
        if not ok:
            errors.append(f"{key}: {normalized}")
            continue
        changes[key] = normalized
        preserve.pop(key, None)


def _fold_routing(answers, *, routing, data, errors) -> None:
    preset = answers.get("preset")
    if preset is None:
        return
    preset = str(preset)
    if preset == "custom":
        _plan_explicit_choice(
            role=answers.get("role"), provider=answers.get("provider"),
            model=answers.get("model"), routing=routing, errors=errors,
        )
        return
    choices = _preset_choices(preset, data)
    if not choices:
        errors.append(f"routing preset {preset!r} is not declared in the model registry")
        return
    routing["preset"] = preset
    routing["choices"] = choices


def _default_ask(*, state: Mapping[str, object], flags: dict):
    """Build the private adapter over :mod:`pipeline.setup.ui`."""

    def ask(name: str, current: Mapping[str, str]) -> dict:
        if name == "core":
            return _ask_core(current, flags=flags)
        if name == "routing":
            return _ask_routing(current, state=state)
        if name == "policy":
            answers = _ask_keys(_POLICY_PROMPTS, current)
            if ui.ask_yes_no("Customize the advanced gates?", default=False):
                kinds = dict.fromkeys(_ADVANCED_POLICY_KEYS, "text")
                answers.update(_ask_keys(_ADVANCED_POLICY_KEYS, current, kinds=kinds))
            return answers
        if name == "notifications":
            return _ask_notifications(current)
        if name == "integrations":
            return _ask_integrations(current)
        if name == "claude_assets":
            return {
                "install": ui.ask_yes_no("Install the Claude Code assets?", default=True)
            }
        if name == "services":
            # Only "start now" defaults to yes, and only in interactive mode.
            return {
                "render": ui.ask_yes_no("Render the service definitions?", default=False),
                "enable": ui.ask_yes_no("Enable the scheduler at login?", default=False),
                "start": ui.ask_yes_no("Start the dashboard and scheduler now?", default=True),
                "port": current.get("port", str(DEFAULT_PORT)),
            }
        return {}

    return ask


def _ask_keys(keys: Sequence[str], current, *, kinds: Mapping | None = None) -> dict:
    kinds = kinds or {}
    answers: dict[str, str] = {}
    for key in keys:
        spec = _SPECS[key]
        default = current.get(key) or spec.default or ""
        kind = kinds.get(key) or ("choice" if spec.choices else "text")
        if kind == "choice" and default not in spec.choices:
            default = spec.choices[0]
        answers[key] = ui.prompt_value(
            _prompt_label(key), kind="secret" if key in _SECRETS else kind,
            default=default, choices=spec.choices,
            validate=lambda raw, k=key: _schema.validate_value(k, raw),
        )
    return answers


def _ask_core(current: Mapping[str, str], *, flags: dict) -> dict:
    """Ask the curated core questions."""
    data_dir = ui.prompt_value(
        "Data directory (plans and worktrees live under it)", kind="text",
        default=DEFAULT_DATA_DIR,
        validate=lambda raw: _schema.validate_value("PLAN_DIR", raw),
    )
    answers: dict[str, str] = {}
    if data_dir:
        answers["PLAN_DIR"] = str(Path(data_dir) / "plans")
        answers["WORKTREE_ROOT"] = str(Path(data_dir) / "worktrees")
    if _offers_repo_root():
        target = ui.prompt_value(
            "Target project directory (blank leaves REPO_ROOT unset)", kind="text",
            default="",
            validate=lambda raw: _schema.validate_value("REPO_ROOT", raw)
            if raw else (True, ""),
        )
        if target:
            answers["REPO_ROOT"] = target
    answers["port"] = ui.prompt_value(
        "Dashboard port", kind="text", default=str(flags.get("port", DEFAULT_PORT)),
        validate=lambda raw: _schema.validate_value("PIPELINE_NOTIFY_EMAIL_PORT", raw),
    )
    return answers


def _offers_repo_root() -> bool:
    try:
        here, checkout = Path.cwd().resolve(), Path(__file__).resolve().parents[2]
    except (OSError, IndexError):
        return False
    return (here / ".git").exists() and here != checkout


def _ask_routing(current: Mapping[str, str], *, state: Mapping[str, object]) -> dict:
    """Ask the preset, and for ``custom`` one role plus a declared pair."""
    preset = ui.prompt_value(
        "Routing preset", kind="choice",
        default=current.get("preset") or DEFAULT_PRESET, choices=providers.PRESETS,
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
    chosen = ui.prompt_value("Provider/model", kind="choice", default=labels[0],
                             choices=labels)
    provider, _, model = str(chosen).partition("/")
    return {"preset": "custom", "role": role, "provider": provider, "model": model}


def _declared_pairs(data: Mapping[str, object]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    declared = data.get("providers") if isinstance(data, Mapping) else None
    if not isinstance(declared, Mapping):
        return pairs
    for provider, spec in declared.items():
        models = spec.get("models") if isinstance(spec, Mapping) else None
        if isinstance(models, Mapping):
            pairs.extend((str(provider), str(model)) for model in models)
    return pairs


def _ask_notifications(current: Mapping[str, str]) -> dict:
    """Ask the outbox gate, then the email gate and its settings."""
    answers: dict[str, str] = {}
    outbox = ui.ask_yes_no("Write pipeline events to the outbox?",
                           default=current.get("PIPELINE_NOTIFY_OUTBOX_ENABLED") == "1")
    answers["PIPELINE_NOTIFY_OUTBOX_ENABLED"] = "1" if outbox else "0"
    if outbox:
        answers.update(_ask_keys(("PIPELINE_NOTIFY_OUTBOX_EVENTS",), current))
    email = ui.ask_yes_no(
        "Send notification emails?",
        default=current.get("PIPELINE_NOTIFY_EMAIL_ENABLED") == "1",
    )
    answers["PIPELINE_NOTIFY_EMAIL_ENABLED"] = "1" if email else "0"
    if email:
        answers.update(_ask_keys(_EMAIL_PROMPTS, current))
    return answers


def _ask_integrations(current: Mapping[str, str]) -> dict:
    """Ask the Plane opt-in, then its settings and the ticket provider."""
    if not ui.ask_yes_no("Enable the Plane integration?", default=False):
        return _ask_keys(_INTEGRATION_PROMPTS, current)
    answers = _ask_keys(_PLANE_PROMPTS, current)
    answers["PIPELINE_TICKET_PROVIDER"] = "plane"
    return answers


def _prompt_label(key: str) -> str:
    spec = _SPECS.get(key)
    return spec.prompt if spec is not None and spec.prompt else key


# -- run_plan ---------------------------------------------------------------


def run_plan(
    plan: Mapping[str, object], *, output: Callable[[str], None] = print,
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
    changed, and then routing, the Claude assets, and the services are applied
    in that order — env first, so a started process sources the new file. A
    failure in any step is recorded in ``errors`` and the remaining steps run.
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
            changes=changes, sections=sections, output=output, next_steps=next_steps
        )

    result: dict[str, object] = {
        "applied": True, "changed": _masked(changes), "env": None,
        "registry": None, "assets": None, "services": None,
        "skipped": [], "errors": [], "next_steps": next_steps,
        "sections": [dict(entry) for entry in sections],
    }
    skipped: list[str] = result["skipped"]  # type: ignore[assignment]
    failures: list[str] = result["errors"]  # type: ignore[assignment]
    enabled = {entry.get("name"): bool(entry.get("enabled")) for entry in sections}

    # (a) the env file, once.
    writer = env_writer if env_writer is not None else envfile.write_env_file
    try:
        result["env"] = writer(
            _env_path(flags, state),
            {**preserve, **changes},
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
        result["registry"] = _run_step(
            "applying routing", applier, failures,
            choices=choices, flags=flags, state=state,
        )
    else:
        skipped.append("routing")

    # (d) the Claude assets and the MCP registration.
    if enabled.get("claude_assets"):
        installer = (
            asset_installer if asset_installer is not None else _default_asset_installer
        )
        result["assets"] = _run_step(
            "installing the Claude assets", installer, failures, flags=flags, state=state
        )
    else:
        skipped.append("claude_assets")

    # (e) the services, last so they source the new env file.
    if enabled.get("services"):
        runner = service_runner if service_runner is not None else _default_service_runner
        result["services"] = _run_step(
            "starting the services", runner, failures, flags=flags, state=state
        )
        if result["services"] is not None:
            _record_service_health(result, flags, failures)
    else:
        skipped.append("services")

    for entry in sections:
        name = entry.get("name")
        if not entry.get("enabled") and name not in skipped:
            skipped.append(name)

    _record_applier_failures(result, failures)
    return result


def _run_step(label: str, fn, failures: list[str], **kwargs):
    try:
        return fn(**kwargs)
    except Exception as exc:  # noqa: BLE001 - a host failure is data
        failures.append(f"{label} failed: {exc}")
        return None


def _repo_root(flags: Mapping, state: Mapping | None) -> str:
    from_state = None if state is None else _existing_env(state).get("REPO_ROOT")
    return flags.get("repo_root") or from_state or str(Path.cwd())


def _env_path(flags: Mapping, state: Mapping | None) -> Path:
    return Path(_repo_root(flags, state)) / ENV_FILE_NAME


def _dry_run(*, changes, sections, output, next_steps) -> dict:
    for key, value in changes.items():
        output(f"{key}={_mask(key, value)}")
    for entry in sections:
        name, reason = entry.get("name"), entry.get("reason") or "disabled"
        if entry.get("enabled"):
            output(f"section {name}: enabled")
        else:
            output(f"section {name}: skipped ({reason})")
    return {
        "applied": False, "dry_run": True, "planned": _masked(changes),
        "sections": [dict(entry) for entry in sections],
        "next_steps": list(next_steps), "errors": [],
    }


def _create_directories(changes: Mapping[str, str], failures: list[str]) -> None:
    for key in ("PLAN_DIR", "WORKTREE_ROOT"):
        if not changes.get(key):
            continue
        try:
            Path(changes[key]).mkdir(parents=True, exist_ok=True, mode=0o755)
        except OSError as exc:
            failures.append(f"creating {key} failed: {exc}")


def _default_applier(*, choices, flags, state):
    registry_path = providers.ensure_local_registry(_repo_root(flags, state))
    return providers.apply_choices(choices, registry_path=registry_path)


def _default_asset_installer(*, flags, state):
    repo_root = _repo_root(flags, state)
    return {
        "assets": claude_setup.install_claude_assets(repo_root=repo_root),
        "mcp": claude_setup.register_mcp_server(repo_root=repo_root),
    }


def _default_service_runner(*, flags, state):
    repo_root = _repo_root(flags, state)
    return {
        "render": services.render_services(
            repo_root=repo_root, platform=None, mlx_model_path=None, enable=False
        ),
        "start": services.start_services(
            repo_root=repo_root, port=int(flags.get("port") or DEFAULT_PORT)
        ),
    }


def _record_service_health(result: dict, flags: Mapping, failures: list[str]) -> None:
    started = result.get("services")
    start = started.get("start") if isinstance(started, Mapping) else None
    dashboard = start.get("dashboard") if isinstance(start, Mapping) else None
    if not isinstance(dashboard, Mapping) or not dashboard.get("ok"):
        return
    try:
        health = _verify_health(int(flags.get("port") or DEFAULT_PORT))
    except Exception as exc:  # noqa: BLE001 - a host failure is data
        failures.append(f"verifying the dashboard health failed: {exc}")
        return
    result["health"] = health
    if not health.get("healthy"):
        reason = str(health.get("reason") or "unknown reason")
        failures.append(f"the dashboard did not answer its health check: {reason}")


def _verify_health(port: int) -> dict:
    """Probe the dashboard, obtaining the shared secret lazily.

    The secret is passed to :func:`pipeline.setup.services.verify_health` and
    never stored in the returned dict.
    """
    from app.auth import get_or_create_api_key

    healthy, reason = services.verify_health(
        port=port, api_key=get_or_create_api_key()
    )
    return {"healthy": bool(healthy), "reason": reason}


def _record_applier_failures(result: dict, failures: list[str]) -> None:
    for block, label in (
        (_mcp_block(result), "registering the MCP server"),
        (_render_block(result), "rendering the service definitions"),
    ):
        if block is not None and block.get("status") == "failed":
            error = str(block.get("error") or "unknown error")
            failures.append(f"{label} failed: {error}")


# -- render_summary ---------------------------------------------------------


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
            name, reason = entry.get("name"), entry.get("reason") or "disabled"
            if entry.get("enabled"):
                lines.append(f"  {name}: enabled")
            else:
                lines.append(f"  {name}: skipped ({reason})")
        lines.append("")

    changed = dict(result.get("changed") or result.get("planned") or {})
    if changed:
        lines.append("Changes:")
        lines.extend(f"  {key}={_mask(key, value)}" for key, value in changed.items())
        lines.append("")

    errors = list(result.get("errors") or ())
    if errors:
        lines.append("Errors:")
        lines.extend(f"  {error}" for error in errors)
        lines.append("")
    if _summary_autonomy(result) == "dry-run":
        lines.extend([describe_autonomy("dry-run"), ""])

    lines.append("Next steps:")
    lines.extend(f"  - {step}" for step in _summary_next_steps(result))
    return "\n".join(lines).rstrip() + "\n"


def _summary_autonomy(result: Mapping[str, object]) -> str | None:
    for field in ("changed", "planned"):
        values = result.get(field)
        value = values.get("PIPELINE_AUTONOMY") if isinstance(values, Mapping) else None
        if isinstance(value, str):
            return value
    return None


def _mcp_block(result: Mapping[str, object]) -> Mapping | None:
    assets = result.get("assets")
    mcp = assets.get("mcp") if isinstance(assets, Mapping) else None
    return mcp if isinstance(mcp, Mapping) else None


def _render_block(result: Mapping[str, object]) -> Mapping | None:
    started = result.get("services")
    render = started.get("render") if isinstance(started, Mapping) else None
    return render if isinstance(render, Mapping) else None


def _summary_next_steps(result: Mapping[str, object]) -> list[str]:
    steps = [str(step) for step in result.get("next_steps") or ()]
    mcp = _mcp_block(result)
    if mcp is not None and mcp.get("status") in ("failed", "skipped"):
        manual = mcp.get("manual") or MCP_MANUAL_COMMAND
        steps.append(f"Register the MCP server by hand: {manual}")
    return steps + [RESTART_STEP, TEST_STEP]
