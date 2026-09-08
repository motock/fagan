"""Runtime environment validation for the pipeline ("can this host run it?").

run_preflight() performs four checks -- PLAN_DIR, git, the dispatch backend,
and the model registry -- and returns one dict per check:

    {"name": ..., "status": "ok" | "warn" | "fail", "message": ...}

summarize() renders the results as one human line, and raise_on_failure()
turns fail-status checks into a PreflightError (warns never raise).

Messages are actionable but non-leaking: they name paths (PLAN_DIR,
model_registry.json) and error *classes*, never env values, tokens, loader
error text, or file contents. `which` and `registry_loader` are injectable so
tests can stub the host's installed tools and today's registry contents
(test the resolution logic, not today's configured values).

Module-top imports are stdlib-only plus pipeline.paths (stdlib-only itself);
nothing is imported from app/.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

# pipeline.paths is stdlib-only (verified: importing it pulls no third-party
# modules), so its PLAN_DIR binding is reused as the repo's documented patch
# surface for plan-dir resolution. It is read at call time, never cached here.
from pipeline import paths

__all__ = ["PreflightError", "raise_on_failure", "run_preflight", "summarize"]

# Backends in the "local family": the operator explicitly opted into local
# dispatch, so a missing provider CLI degrades to a warn (graceful degradation),
# never a fail and never a silent pass.
_LOCAL_BACKENDS = frozenset({"ollama", "lmstudio", "mlx", "local", "auto"})

# Sentinel default_provider handed to role_registry.resolve_role(): it comes
# back only when NO source configured the dispatch role (no plan config, no
# PIPELINE_BACKEND_DISPATCH env var, no registry roles.dispatch entry). The
# check then warns instead of presenting the built-in default ("claude") as
# though the operator had chosen it. Lowercase so resolve_role's own
# .strip().lower() normalization cannot mangle the comparison.
_UNCONFIGURED_PROVIDER = "_no_dispatch_provider_configured_"


class PreflightError(RuntimeError):
    """Raised by raise_on_failure() when any check reports status "fail"."""


def run_preflight(plan_dir=None, which=shutil.which, registry_loader=None):
    """Run the four preflight checks; return one result dict per check.

    plan_dir: explicit plan directory to check. None resolves the way
        production does: the PLAN_DIR env var (read at call time), then
        pipeline.paths.PLAN_DIR (default ~/.claude/plans).
    which: shutil.which-compatible callable (injectable so tests never depend
        on the live host's installed CLIs).
    registry_loader: zero-arg callable returning the parsed model registry
        (injectable so tests never depend on today's model_registry.json).
        None uses the built-in loader, which json-parses model_registry.json
        directly from the repo root.

    The checks are read-only: nothing is created, written, or executed.
    """

    def _effective_dispatch_check(which):
        """Check c, production path: report the EFFECTIVE dispatch resolution.

        Resolves the dispatch role exactly the way production does via
        app.role_registry.resolve_role (plan_role_config -> PIPELINE_BACKEND_
        DISPATCH env -> registry roles.dispatch -> default_provider), imported
        lazily so a raising registry module degrades to a fail check instead
        of breaking preflight import. Non-leaking like every check here: on
        resolution failure the message carries the exception CLASS name only —
        never str(exc), env values, or file paths.
        """
        try:
            from app import role_registry  # lazy: see docstring above
        except Exception as exc:  # noqa: BLE001 - any import failure is a fail
            return {
                "name": "dispatch backend",
                "status": "fail",
                "message": (
                    f"dispatch role resolution unavailable "
                    f"({type(exc).__name__}) — check that the role registry "
                    "module is importable and model_registry.json is present "
                    "and readable"
                ),
            }

        try:
            # Normalize the env var BEFORE resolve_role sees it: a blank or
            # whitespace-only value must count as unset (registry consulted),
            # while resolve_role's own `or` chain would treat whitespace as
            # truthy and resolve provider to "". The explicit environ dict
            # also keeps every other PIPELINE_* var out of the resolution.
            # The fallback mirrors pipeline.config.DEFAULT_MODEL (read at
            # call time, like every env read in this module).
            raw_env = os.environ.get("PIPELINE_BACKEND_DISPATCH", "")
            normalized = raw_env.strip().lower()
            resolution = role_registry.resolve_role(
                "dispatch",
                model_fallback=lambda: os.environ.get(
                    "PIPELINE_DEFAULT_MODEL", "sonnet"
                ),
                default_provider=_UNCONFIGURED_PROVIDER,
                environ=(
                    {"PIPELINE_BACKEND_DISPATCH": normalized}
                    if normalized
                    else {}
                ),
            )
        except Exception as exc:  # noqa: BLE001 - any resolution failure is a fail
            return {
                "name": "dispatch backend",
                "status": "fail",
                "message": (
                    f"dispatch role failed to resolve ({type(exc).__name__}) "
                    "— check the PIPELINE_BACKEND_DISPATCH env var and the "
                    "roles block in model_registry.json"
                ),
            }

        provider = resolution.provider
        model = resolution.model

        if provider == _UNCONFIGURED_PROVIDER:
            # Nothing configured the dispatch role from any source. Never
            # present the built-in default as though the operator chose it.
            return {
                "name": "dispatch backend",
                "status": "warn",
                "message": (
                    "no provider is configured for the dispatch role — "
                    "dispatch would fall back to a built-in default. Choose "
                    "one: set PIPELINE_BACKEND_DISPATCH (e.g. ollama, "
                    "lmstudio, mlx, local), or add a roles.dispatch "
                    "provider/model entry to model_registry.json"
                ),
            }

        cli_name = "lms" if provider == "lmstudio" else provider
        cli_path = which(cli_name)
        if cli_path:
            return {
                "name": "dispatch backend",
                "status": "ok",
                "message": (
                    f"dispatch resolves to {provider}/{model} — CLI "
                    f"'{cli_name}' found: {cli_path}"
                ),
            }
        if provider == "claude":
            # Claude stays a hard requirement (matching the injected-loader
            # contract): a missing Claude Code CLI is a fail, never a warn.
            return {
                "name": "dispatch backend",
                "status": "fail",
                "message": (
                    f"dispatch resolves to {provider}/{model} but the Claude "
                    "Code CLI was not found on PATH — install the Claude "
                    "Code CLI, or set PIPELINE_BACKEND_DISPATCH to an "
                    "available backend"
                ),
            }
        # Every other resolved provider degrades gracefully (local-family
        # behavior): a missing CLI is a warn, never a fail, never a crash.
        return {
            "name": "dispatch backend",
            "status": "warn",
            "message": (
                f"dispatch resolves to {provider}/{model} but its CLI "
                f"('{cli_name}') was not found on PATH — dispatch will fail "
                f"until the provider is installed/configured (install "
                f"{cli_name}, or set PIPELINE_BACKEND_DISPATCH)"
            ),
        }

    results = []

    # -- check a: PLAN_DIR --------------------------------------------------
    # Resolve exactly the way production does; explicit argument wins.
    if plan_dir is not None:
        plan_path = Path(plan_dir).expanduser()
    else:
        env_plan_dir = os.environ.get("PLAN_DIR")
        if env_plan_dir:
            plan_path = Path(env_plan_dir).expanduser()
        else:
            plan_path = Path(paths.PLAN_DIR).expanduser()

    if plan_path.exists():
        if not plan_path.is_dir():
            results.append({
                "name": "PLAN_DIR",
                "status": "fail",
                "message": (
                    f"PLAN_DIR exists but is not a directory: {plan_path} — "
                    "remove it or point PLAN_DIR at a directory"
                ),
            })
        elif os.access(plan_path, os.W_OK):
            results.append({
                "name": "PLAN_DIR",
                "status": "ok",
                "message": f"plan dir is writable: {plan_path}",
            })
        else:
            results.append({
                "name": "PLAN_DIR",
                "status": "fail",
                "message": (
                    f"PLAN_DIR is not writable: {plan_path} — fix its "
                    f"permissions (chmod u+w '{plan_path}') or point PLAN_DIR "
                    "at a writable directory"
                ),
            })
    else:
        # Missing dir: warn only if its parent exists and is writable. Never
        # create it here -- this check must stay side-effect-free.
        parent = plan_path.parent
        if parent.is_dir() and os.access(parent, os.W_OK):
            results.append({
                "name": "PLAN_DIR",
                "status": "warn",
                "message": (
                    "PLAN_DIR does not exist yet (it will be created on "
                    f"first use): {plan_path}"
                ),
            })
        else:
            results.append({
                "name": "PLAN_DIR",
                "status": "fail",
                "message": (
                    f"PLAN_DIR cannot be created: {plan_path} (parent "
                    f"directory {parent} is missing or not writable) — create "
                    "it or point PLAN_DIR at a writable location"
                ),
            })

    # -- check b: git (required) --------------------------------------------
    git_path = which("git")
    if git_path:
        results.append({
            "name": "git",
            "status": "ok",
            "message": f"git found: {git_path}",
        })
    else:
        results.append({
            "name": "git",
            "status": "fail",
            "message": (
                "git is required but was not found on PATH — install git "
                "(e.g. 'brew install git' on macOS or 'apt-get install git' "
                "on Debian/Ubuntu) and re-run preflight"
            ),
        })

    # -- check c: dispatch backend ------------------------------------------
    # With an injected registry_loader (tests, dashboard) this check keeps its
    # original contract: read the env var at call time (never at import time)
    # and normalize it. Production (registry_loader=None) instead reports the
    # EFFECTIVE resolution: app.role_registry.resolve_role's chain is
    # plan_role_config -> PIPELINE_BACKEND_<ROLE> env -> registry roles ->
    # default_provider, so when the env var is unset the registry wins and the
    # raw env default ("claude") would be a false green. The registry module is
    # imported lazily INSIDE the check so preflight keeps working (and keeps
    # its stdlib-only module-top imports) even if that import raises.
    if registry_loader is not None:
        raw_backend = os.environ.get("PIPELINE_BACKEND_DISPATCH", "claude")
        backend = (raw_backend or "claude").strip().lower() or "claude"

        if backend == "claude":
            claude_path = which("claude")
            if claude_path:
                results.append({
                    "name": "dispatch backend",
                    "status": "ok",
                    "message": f"dispatch backend 'claude' found: {claude_path}",
                })
            else:
                results.append({
                    "name": "dispatch backend",
                    "status": "fail",
                    "message": (
                        "dispatch backend 'claude' requires the Claude Code "
                        "CLI, which was not found on PATH — install the "
                        "Claude Code CLI, or set PIPELINE_BACKEND_DISPATCH "
                        "to an available backend"
                    ),
                })
        elif backend in _LOCAL_BACKENDS:
            # Local family: graceful degradation. A missing provider CLI is a
            # warn (dispatch will fail until the provider is installed), never
            # a fail and never a crash.
            cli_name = "lms" if backend == "lmstudio" else backend
            cli_path = which(cli_name)
            if cli_path:
                results.append({
                    "name": "dispatch backend",
                    "status": "ok",
                    "message": f"dispatch backend '{backend}' found: {cli_path}",
                })
            else:
                results.append({
                    "name": "dispatch backend",
                    "status": "warn",
                    "message": (
                        f"dispatch backend '{backend}' selected but its CLI "
                        f"('{cli_name}') was not found on PATH — dispatch "
                        f"will fail until the provider is installed/configured "
                        f"(install {cli_name}, or switch PIPELINE_BACKEND_DISPATCH)"
                    ),
                })
        else:
            # Unrecognized value: never crash, never pass silently.
            results.append({
                "name": "dispatch backend",
                "status": "warn",
                "message": (
                    f"unknown dispatch backend '{backend}' "
                    "(PIPELINE_BACKEND_DISPATCH) — dispatch will fail until "
                    "it is set to a known backend (claude, ollama, lmstudio, "
                    "mlx, local, auto) or the provider is installed/configured"
                ),
            })
    else:
        results.append(
            _effective_dispatch_check(which)
        )

    # -- check d: model registry --------------------------------------------
    try:
        if registry_loader is not None:
            registry_payload = registry_loader()
        else:
            # pipeline/role_registry.py does not exist in this repo (and the
            # brief forbids heavy imports here), so the model registry is
            # loaded by json-parsing model_registry.json directly from the
            # repo root. Only the parse result's *shape* is inspected — never
            # its contents, which must not leak into messages.
            registry_path = (
                Path(__file__).resolve().parents[1] / "model_registry.json"
            )
            registry_payload = json.loads(
                registry_path.read_text(encoding="utf-8")
            )
    except Exception as exc:  # noqa: BLE001 - any loader failure is a fail
        # Non-leaking: report the error CLASS name only. Never embed str(exc)
        # / repr(exc) (they may carry env values or tokens), env values, or
        # file contents — just a static hint pointing at model_registry.json.
        results.append({
            "name": "model registry",
            "status": "fail",
            "message": (
                f"model registry failed to load ({type(exc).__name__}) — "
                "check that model_registry.json exists, is valid JSON, and "
                "is readable"
            ),
        })
    else:
        if isinstance(registry_payload, dict):
            results.append({
                "name": "model registry",
                "status": "ok",
                "message": "model registry loaded and well-formed",
            })
        else:
            results.append({
                "name": "model registry",
                "status": "fail",
                "message": (
                    "model registry at model_registry.json is malformed: "
                    "expected a JSON object at the top level, got "
                    f"{type(registry_payload).__name__}"
                ),
            })

    return results


def summarize(results):
    """Render preflight results as one human line.

    The parenthetical (when any check warned) carries check NAMES only —
    never check messages, which may contain absolute paths or other values
    that must not be echoed into logs.
    """
    counts = {"ok": 0, "warn": 0, "fail": 0}
    warn_names = []
    for check in results:
        status = check.get("status") if isinstance(check, dict) else None
        if status in counts:
            counts[status] += 1
            if status == "warn":
                warn_names.append(str(check.get("name", "unnamed")))
    summary = (
        f"preflight: {counts['ok']} ok, {counts['warn']} warn, "
        f"{counts['fail']} fail"
    )
    if warn_names:
        summary += f" ({', '.join(warn_names)})"
    return summary


def raise_on_failure(results):
    """Raise PreflightError listing every fail-status check; warns never raise."""
    failures = [
        check
        for check in results
        if isinstance(check, dict) and check.get("status") == "fail"
    ]
    if not failures:
        return
    details = "; ".join(
        f"{check.get('name', 'unnamed')}: {check.get('message', '')}"
        for check in failures
    )
    raise PreflightError(
        f"preflight failed ({len(failures)} check(s)): {details}"
    )