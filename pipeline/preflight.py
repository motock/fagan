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


class PreflightError(RuntimeError):
    """Raised by raise_on_failure() when any check reports status "fail"."""


def _check_scheduler_config(plan_dir, worktree_root):
    """Compare the scheduler's config fingerprint against THIS process's.

    Reads ``<plan_dir>/.scheduler_health.json`` (the CFG-B2 path the
    scheduler daemon writes) and diffs its ``config.plan_dir`` /
    ``config.worktree_root`` against the values run_preflight() already
    resolved — the same comparison /api/health performs (CFG-B3), surfaced
    at startup instead of only on demand.

    Outcomes:
        agreement              -> "ok"   (message names the matching plan dir)
        divergence             -> "warn" (NOT "fail": a divergence must never
                                          block startup; the message names
                                          both values and which field differs)
        file absent            -> "ok"   (a standalone or not-yet-started
                                          scheduler is a normal state, not a
                                          problem; message says none found)
        malformed / unreadable -> "warn" (never an exception)

    Read-only: the fingerprint is never created, written, or removed here.
    """
    fingerprint_path = os.path.join(str(plan_dir), ".scheduler_health.json")
    try:
        if not os.path.exists(fingerprint_path):
            return {
                "name": "SCHEDULER_CONFIG",
                "status": "ok",
                "message": (
                    "no scheduler fingerprint found at "
                    f"{fingerprint_path} (a standalone or not-yet-started "
                    "scheduler is normal)"
                ),
            }
        with open(fingerprint_path, "r", encoding="utf-8") as handle:
            payload = json.loads(handle.read())
        if not isinstance(payload, dict) or not isinstance(
            payload.get("config"), dict
        ):
            raise TypeError("fingerprint carries no usable config object")
        fingerprint = payload["config"]
        fp_plan_dir = fingerprint["plan_dir"]
        fp_worktree_root = fingerprint["worktree_root"]
    except Exception as exc:  # noqa: BLE001 - any fingerprint problem is a warn
        # Non-leaking: report the error CLASS name only, never str(exc) /
        # repr(exc) (they may carry env values or file contents).
        return {
            "name": "SCHEDULER_CONFIG",
            "status": "warn",
            "message": (
                f"scheduler fingerprint at {fingerprint_path} is malformed "
                f"or unreadable ({type(exc).__name__}) — cannot compare "
                "scheduler config against this process"
            ),
        }

    problems = []
    if str(fp_plan_dir) != str(plan_dir):
        problems.append(
            f"plan_dir: fingerprint={fp_plan_dir} resolved={plan_dir}"
        )
    if worktree_root is not None and str(fp_worktree_root) != str(
        worktree_root
    ):
        problems.append(
            f"worktree_root: fingerprint={fp_worktree_root} "
            f"resolved={worktree_root}"
        )
    if problems:
        return {
            "name": "SCHEDULER_CONFIG",
            "status": "warn",
            "message": (
                "scheduler config divergence: " + "; ".join(problems)
            ),
        }
    return {
        "name": "SCHEDULER_CONFIG",
        "status": "ok",
        "message": f"scheduler config matches resolved plan dir: {plan_dir}",
    }


def _check_scheduler_revision(plan_dir):
    """Report whether the RUNNING scheduler is executing current code.

    Reads the ``config.checkout_sha`` / ``config.checkout_behind_origin`` the
    scheduler daemon records in the same ``<plan_dir>/.scheduler_health.json``
    the config check reads. The daemon measures both against its own checkout's
    upstream, so this compares like with like and never needs git here.

    Outcomes:
        behind_origin == 0  -> "ok"   (the daemon runs its checkout's revision)
        behind_origin  > 0  -> "warn" (names the count: the merge landed, but
                                       the checkout was not pulled/restarted)
        keys absent         -> "warn" (the running daemon predates the revision
                                       keys, so it is executing old code)
        file absent         -> "ok"   (a standalone or not-yet-started
                                       scheduler is a normal state)
        malformed/unreadable-> "warn" (never an exception)
        value None          -> "ok"   (git could not answer, e.g. no upstream
                                       configured; an unclearable warn would
                                       only train operators to ignore these)

    A stale revision is a warn, never a fail: it must not block startup.
    Read-only: the fingerprint is never created, written, or removed here.
    """
    fingerprint_path = os.path.join(str(plan_dir), ".scheduler_health.json")
    try:
        if not os.path.exists(fingerprint_path):
            return {
                "name": "SCHEDULER_REVISION",
                "status": "ok",
                "message": (
                    "no scheduler fingerprint found at "
                    f"{fingerprint_path} (a standalone or not-yet-started "
                    "scheduler is normal)"
                ),
            }
        with open(fingerprint_path, "r", encoding="utf-8") as handle:
            payload = json.loads(handle.read())
        if not isinstance(payload, dict) or not isinstance(
            payload.get("config"), dict
        ):
            raise TypeError("fingerprint carries no usable config object")
        fingerprint = payload["config"]
    except Exception as exc:  # noqa: BLE001 - any fingerprint problem is a warn
        # Non-leaking: report the error CLASS name only, never str(exc) /
        # repr(exc) (they may carry env values or file contents).
        return {
            "name": "SCHEDULER_REVISION",
            "status": "warn",
            "message": (
                f"scheduler fingerprint at {fingerprint_path} is malformed "
                f"or unreadable ({type(exc).__name__}) — cannot tell which "
                "revision the running scheduler is executing"
            ),
        }

    if "checkout_sha" not in fingerprint:
        return {
            "name": "SCHEDULER_REVISION",
            "status": "warn",
            "message": (
                "the running scheduler does not report a checkout revision "
                f"(fingerprint at {fingerprint_path}) — it is executing code "
                "from before that field existed; pull the checkout and "
                "restart the scheduler so it runs the current code"
            ),
        }
    behind = fingerprint.get("checkout_behind_origin")
    sha = fingerprint.get("checkout_sha")
    if isinstance(behind, int) and behind > 0:
        return {
            "name": "SCHEDULER_REVISION",
            "status": "warn",
            "message": (
                f"the running scheduler is {behind} commit(s) behind its "
                "checkout's upstream — pull the checkout and restart the "
                "scheduler to run the current code"
            ),
        }
    if behind == 0:
        return {
            "name": "SCHEDULER_REVISION",
            "status": "ok",
            "message": f"running scheduler is on its checkout's revision ({sha})",
        }
    return {
        "name": "SCHEDULER_REVISION",
        "status": "ok",
        "message": (
            f"running scheduler revision is {sha}; its distance from upstream "
            "could not be measured"
        ),
    }


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
    # REG-3: resolve the backend the SAME way real per-story dispatch does --
    # pipeline/dispatch.py's _resolve_dispatch_target, which since REG-1/REG-2
    # resolves the dispatch role through app.role_registry.resolve_role
    # (priority: plan role_config -> PIPELINE_BACKEND_DISPATCH -> registry
    # roles.dispatch.provider -> "claude", with dispatch's own fail-open
    # fallback when the registry cannot resolve the role). Reading the env
    # var raw here -- the PP-02 review's resolution -- reported a backend
    # real dispatch would never run on a registry-pinned host: the same
    # false-green defect class this check exists to prevent, pointing the
    # other way. The resolver is imported and called fresh at call time
    # (never at import time, never cached, nothing written back to the env
    # or the registry), so the two can never drift apart again.
    try:
        from pipeline.dispatch import _resolve_dispatch_target

        backend = _resolve_dispatch_target({}, None)[0]
    except Exception:  # noqa: BLE001 -- a diagnostic must never crash
        # Only reachable when the shared resolver itself is unavailable
        # (e.g. a broken app.role_registry import). Degrade to dispatch's
        # own documented fail-open priority -- the env var, then "claude" --
        # exactly what _dispatch_fallback_provider would resolve to.
        raw_backend = os.environ.get("PIPELINE_BACKEND_DISPATCH", "claude")
        backend = (raw_backend or "claude").strip().lower() or "claude"
    backend = (backend or "claude").strip().lower() or "claude"

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
                    "dispatch backend 'claude' requires the Claude Code CLI, "
                    "which was not found on PATH — install the Claude Code "
                    "CLI, or set PIPELINE_BACKEND_DISPATCH to an available "
                    "backend"
                ),
            })
    elif backend in _LOCAL_BACKENDS:
        # Local family: graceful degradation. A missing provider CLI is a
        # warn (dispatch will fail until the provider is installed), never a
        # fail and never a crash.
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
                    f"('{cli_name}') was not found on PATH — dispatch will "
                    f"fail until the provider is installed/configured "
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
                "(PIPELINE_BACKEND_DISPATCH) — dispatch will fail until it "
                "is set to a known backend (claude, ollama, lmstudio, mlx, "
                "local, auto) or the provider is installed/configured"
            ),
        })

    # -- check c2: merge CI gate ---------------------------------------------
    # Read the env var at call time (never at import time), like the
    # dispatch-backend check above: an operator can disable or re-enable the
    # gate between preflight runs, and an import-time read would report a
    # stale state. Warn, never fail: disabling the gate is a legitimate
    # operator choice during a genuine CI outage, and preflight failing hard
    # would block work during exactly the outage the escape hatch exists
    # for. It must be impossible to MISS, not impossible to DO.
    if os.environ.get("PIPELINE_MERGE_CI_GATE", "1").strip() == "0":
        results.append({
            "name": "merge CI gate",
            "status": "warn",
            "message": (
                "PIPELINE_MERGE_CI_GATE=0: the merge gate will NOT consult "
                "CI, so merges can land on red. Unset PIPELINE_MERGE_CI_GATE "
                "to restore the gate. This is usually a temporary workaround "
                "(e.g. a CI outage) and should be removed once CI is green "
                "again."
            ),
        })
    else:
        results.append({
            "name": "merge CI gate",
            "status": "ok",
            "message": "merge gate requires green CI before merging",
        })

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

    # -- check e: scheduler config fingerprint --------------------------------
    # CFG-B4: surface dashboard/scheduler config divergence at STARTUP, not
    # only on demand (/api/health). Compares the fingerprint the scheduler
    # daemon writes to <plan_dir>/.scheduler_health.json against the values
    # resolved above (plan_path) and by this process. WORKTREE_ROOT is read
    # at call time exactly the way PLAN_DIR is resolved above; when the
    # operator has set it this is identical to pipeline.paths.WORKTREE_ROOT
    # (env-or-default), and when it is unset the field is not compared (the
    # built-in default is not a configured value a scheduler must agree with).
    # A divergence is a warn, never a fail: it must not block startup
    # (raise_on_failure() raises only on fail-status checks).
    env_worktree_root = os.environ.get("WORKTREE_ROOT")
    worktree_root = (
        Path(env_worktree_root).expanduser() if env_worktree_root else None
    )
    results.append(_check_scheduler_config(plan_path, worktree_root))

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