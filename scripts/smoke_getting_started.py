"""Scratch-PLAN_DIR getting-started smoke: run ONE story to tests_passed.

This is the operator-facing "does a fresh checkout actually work?" driver.
It builds a throwaway scratch layout, saves + ingests a minimal 1-epic /
1-story plan against a scratch target git repo, dispatches the story on the
``claude`` backend, and polls until the story's tests pass - all without ever
writing into the operator's real ~/.claude/plans.

WHY THE SUCCESS BAR IS tests_passed, NOT A MERGED PR: the scratch repo's
origin is a LOCAL BARE repo (created by _prepare_scratch_env), so
``gh pr create`` cannot run against it - the pr_open and done statuses are
unreachable by construction. This smoke validates save_plan -> ingest ->
dispatch -> implement -> test gate. It does NOT validate the PR or merge
gate.

Scratch layout (everything under one tmp root):

    <tmp_root>/plans/      PLAN_DIR  (plan JSON + manifest live here)
    <tmp_root>/worktrees/  WORKTREE_ROOT (agent worktrees)
    <tmp_root>/repo/       scratch target git repo (one committed README)

CRITICAL ORDERING: pipeline/paths.py reads PLAN_DIR/WORKTREE_ROOT from
os.environ AT IMPORT TIME, so this script sets those env vars BEFORE the
first pipeline.* import (all pipeline imports are lazy, inside functions,
after the env writes). app.role_registry is imported the same way - lazily,
inside the dispatch resolver, never at module scope.
A fail-closed guard then re-verifies the resolved
``pipeline.paths.PLAN_DIR`` module attribute (not the env var - a stale
cached module would make an env-only check lie) is inside the scratch root
and aborts nonzero otherwise.

Exit codes:
    0  PASS - story implemented and its tests passed (status "tests_passed");
       story key + final status printed. It does NOT mean the story merged.
       HONEST CAVEAT: PASS now depends on the CONFIGURED MODEL actually
       completing the story. The smoke no longer pins a provider, so an
       exit 4 on a weak local model means "the model you configured could
       not do it", not "the pipeline is broken" - that is the cost of not
       pinning a provider, and operators must not be surprised by it.
    1  the resolved provider is claude and the ``claude`` CLI is not on PATH
       (install hint printed). Only the claude provider needs the CLI.
    2  the resolved dispatch provider is empty/whitespace-only, or names an
       unknown (unrecognised) provider - a real configuration error, so the
       guard fails closed. The message names the offending value and lists
       the recognised providers (claude, ollama, lmstudio, mlx, local, auto).
       Every DECLARED provider passes after one prominent announce line
       naming the resolved provider, model and source. The guard mirrors the
       resolution real dispatch actually performs
       (pipeline/dispatch.py, pipeline/advance.py, pipeline/preflight.py):
       app.role_registry.resolve_role("dispatch") is the source of truth for
       the dispatch role and outranks the PIPELINE_BACKEND_<ROLE> env var,
       which only fills the empty state; plan/story role_config overrides
       rank above the registry.
    3  the bounded poll timed out (default 30 min, checked every 15 s)
    4  the story reached a terminal failure status (failed/parked)
    5  fail-closed abort: resolved pipeline paths landed outside the scratch

Usage:
    python scripts/smoke_getting_started.py [--check-preconditions]
        [--timeout-s N] [--tmp-root DIR]

``--check-preconditions`` validates the dispatch backend (announcing the
resolved provider/model/source) - plus the claude CLI when claude is the
resolved provider - and exits; it creates nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PLAN_NAME = "smoke-getting-started"
STORY_KEY = "S1"
# Bounded poll: check every 15 seconds (deadline is computed once, from
# time.monotonic(), so an NTP step-back can never stretch the budget).
POLL_INTERVAL_S = 15  # poll every 15 seconds
TERMINAL_FAILURE_STATUSES = ("failed", "parked")


def _prepare_scratch_env(tmp_root: Path | str) -> dict[str, Path]:
    """Create the scratch layout and bind the pipeline env into it.

    Returns a dict with PLAN_DIR, WORKTREE_ROOT and TARGET_REPO (all under
    *tmp_root*). Sets os.environ['PLAN_DIR']/['WORKTREE_ROOT'] BEFORE the
    first pipeline.* import below, then fails closed unless the resolved
    pipeline.paths module attributes are inside the scratch root.
    """
    tmp_root = Path(tmp_root).resolve()
    plan_dir = tmp_root / "plans"
    worktree_root = tmp_root / "worktrees"
    target_repo = tmp_root / "repo"
    plan_dir.mkdir(parents=True, exist_ok=True)
    worktree_root.mkdir(parents=True, exist_ok=True)

    # Scratch target repo: a tiny throwaway repo with one committed README
    # and a REPO-LOCAL git identity (never --global, which would mutate the
    # operator's real gitconfig).
    target_repo.mkdir(parents=True, exist_ok=True)
    (target_repo / "README.md").write_text(
        "# scratch smoke repo\n\n"
        "Throwaway target repo for scripts/smoke_getting_started.py.\n"
    )
    subprocess.run(
        ["git", "init"],
        cwd=str(target_repo),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=str(target_repo),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "--local", "user.name", "Smoke Runner"],
        cwd=str(target_repo),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "--local", "user.email", "smoke@example.invalid"],
        cwd=str(target_repo),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "initial scratch README"],
        cwd=str(target_repo),
        check=True,
        capture_output=True,
        text=True,
    )

    # Real origin: pipeline/dispatch.py builds the agent worktree from
    # origin/<default-branch> (`git fetch origin <branch>`, then
    # `git worktree add -b <branch> <path> origin/<branch>`), so the scratch
    # repo needs a remote it can actually fetch from. That origin is a local
    # BARE repo under the scratch root - which is also why `gh pr create`
    # cannot run against it and this smoke's success bar is tests_passed,
    # not a merged PR (see the module docstring).
    def _git(args: list[str], cwd: Path) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    # Read the ACTUAL current branch: git's init.defaultBranch differs per
    # machine, so it is never assumed to be master or main.
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], target_repo).strip()
    origin = tmp_root / "origin.git"
    _git(["init", "--bare", str(origin)], tmp_root)
    # The bare origin's HEAD symref is set to the SAME branch for
    # consistency, but it is NOT what dispatch reads: pipeline/server.py::
    # _default_branch reads refs/remotes/origin/HEAD in the target repo,
    # which `git remote add` + `git push -u` never creates, so dispatch
    # actually resolves the branch via _default_branch's FALLBACK
    # (`git rev-parse --abbrev-ref HEAD` in the target repo) - the same
    # branch we just read and pushed. Order matters - init bare, then point
    # its HEAD, then remote add, then push - so refs/heads/<branch> exists
    # in the bare repo by the time anything reads its HEAD.
    _git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], origin)
    _git(["remote", "add", "origin", str(origin)], target_repo)
    _git(["push", "-u", "origin", branch], target_repo)

    # CRITICAL ORDERING: these env writes must precede the pipeline import
    # below (pipeline/paths.py reads them at import time).
    os.environ["PLAN_DIR"] = str(plan_dir)
    os.environ["WORKTREE_ROOT"] = str(worktree_root)

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import pipeline.paths

    # Fail closed on the resolved MODULE ATTRIBUTES (not the env vars): in a
    # process where pipeline.paths was already imported elsewhere, the env
    # var would say "scratch" while the attribute still binds the operator's
    # real ~/.claude/plans - only the attribute check catches that.
    scratch = tmp_root
    resolved_plan_dir = Path(pipeline.paths.PLAN_DIR).resolve()
    resolved_worktree_root = Path(pipeline.paths.WORKTREE_ROOT).resolve()
    for name, resolved in (
        ("PLAN_DIR", resolved_plan_dir),
        ("WORKTREE_ROOT", resolved_worktree_root),
    ):
        if not (resolved == scratch or scratch in resolved.parents):
            print(
                f"smoke: FAIL-CLOSED abort: resolved pipeline.paths.{name}="
                f"{resolved} is outside the scratch root {scratch}; refusing "
                "to write any plan (the smoke would hit the operator's real "
                "plans dir).",
                file=sys.stderr,
            )
            raise SystemExit(5)
    print(f"smoke: scratch PLAN_DIR={pipeline.paths.PLAN_DIR}")
    return {
        "PLAN_DIR": plan_dir,
        "WORKTREE_ROOT": worktree_root,
        "TARGET_REPO": target_repo,
    }


def _announce_dispatch_backend(
    value: str | None = None,
    *,
    resolver: Callable[[], tuple[str, str, str]] | None = None,
) -> tuple[str, str, str]:
    """Announce the resolved dispatch provider and proceed; fail closed on junk.

    The smoke is provider-neutral: it decouples from any single provider, so
    the operator chooses. The enemy is SILENCE, not the provider - so this
    guard ANNOUNCES the resolved (provider, model, source) triple on one
    prominent line and proceeds, for every DECLARED provider (claude,
    ollama, lmstudio, mlx, local, auto). Exit 2 is reserved for a genuinely
    unusable value: an empty/whitespace-only string, or an unrecognised
    provider name - a real configuration error that must still fail closed.

    Two modes:

    *value* handed in as a string (pure, no I/O): the legacy env-value
    check. Normalization mirrors the dispatch chain exactly
    (``.strip().lower()`` - see pipeline/dispatch.py and pipeline/advance.py).
    Empty/whitespace-only strings and unknown values exit 2 (naming the
    offending value and listing the recognised providers); every declared
    provider passes. This mode never mutates os.environ.

    *value* left unset (what ``main()``/``run_smoke()`` use): the guard
    resolves the backend the way real dispatch actually does - via
    *resolver* (a callable returning ``(provider, model, source)``; the
    default resolver mirrors pipeline/dispatch.py's
    ``_resolve_dispatch_target`` and pipeline/preflight.py's check c:
    ``app.role_registry.resolve_role("dispatch")`` decides, so the
    registry's roles.dispatch entry is the source of truth for the dispatch
    role and outranks the ``PIPELINE_BACKEND_<ROLE>`` env var, which only
    fills the empty state; plan/story role_config overrides rank above the
    registry, and an unusable registry entry fails open to the env var and
    then "claude" exactly as pipeline/dispatch.py does). Exit 2 when the
    resolved provider is empty/whitespace-only or unrecognised; the printed
    message names the offending value AND where the choice came from so the
    operator can change it. The claude-CLI check is NOT part of this guard:
    it lives with the callers and applies only when the resolved provider
    is claude (a local-backend operator does not need the claude CLI).
    This guard never mutates os.environ.

    Returns the validated (provider, model, source) triple so callers can
    name the validated backend in their own output.
    """
    skip_import = os.environ.get("PIPELINE_SKIP_BACKEND_IMPORT") == "1"
    if skip_import:
        provider = os.environ.get("PIPELINE_BACKEND_DISPATCH", "claude").strip().lower()
        if provider == "":
            provider = "claude"
        if provider == "claude":
            model = os.environ.get("PIPELINE_DEFAULT_MODEL", "sonnet")
        else:
            model = os.environ.get("PIPELINE_LOCAL_MODEL_DEFAULT", os.environ.get("PIPELINE_DEFAULT_MODEL", "sonnet"))
        return provider, model, "skip-backend-import"
    recognized = ("claude", "ollama", "lmstudio", "mlx", "local", "auto")

    def _reject(raw: str, provider: str, source: str) -> None:
        """Fail closed on an unusable dispatch value: print + exit 2."""
        if provider.strip() == "":
            detail = "the resolved provider is empty"
            if raw is not None and raw.strip() == "":
                detail = "the value is empty or whitespace-only"
        else:
            detail = f"unrecognised provider {provider!r}"
        message = (
            f"smoke: unrecognised dispatch backend {raw!r} ({detail}); "
            f"recognised providers: {', '.join(recognized)}"
        )
        print(message, file=sys.stderr)
        print(
            "Fix: set PIPELINE_BACKEND_DISPATCH to one of the recognised "
            "providers above (e.g. PIPELINE_BACKEND_DISPATCH=claude), or "
            "unset it to use the default.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    # Handle env var directly to avoid missing dependency errors
    # (removed to let default resolver handle empty env var correctly)
    if value is not None:
        raw = value
        normalized = raw.strip().lower()
        if normalized == "claude":
            print("smoke: backend guard OK: PIPELINE_BACKEND_DISPATCH resolves to claude")
            return normalized, "", "value argument"
        if normalized == "" or normalized not in recognized:
            _reject(raw, normalized, "value argument")
        print(
            f"smoke: validating dispatch on {normalized} (source: value argument)"
        )
        return normalized, "", "value argument"
    def _default_dispatch_resolver() -> tuple[str, str, str]:
        """Resolve the dispatch backend the way real dispatch actually does.

        Mirrors pipeline/dispatch.py's ``_resolve_dispatch_target`` (the
        resolver pipeline/preflight.py's check c calls fresh): the dispatch
        role is resolved through ``app.role_registry.resolve_role("dispatch")``
        - the registry's roles.dispatch entry is the source of truth for the
        dispatch role and outranks the ``PIPELINE_BACKEND_<ROLE>`` env var,
        which only fills the empty state; plan/story role_config overrides
        rank above the registry. The provenance label comes from
        pipeline/config_provenance.resolve_role_provenance, so the reported
        source names the layer that actually won (registry, env var, plan
        role_config or default) instead of always naming the env var.

        Fail-open contract (same as pipeline/dispatch.py): a fresh clone
        ships model_registry.json with no roles.dispatch entry, and a
        malformed entry raises too - both degrade to the pre-registry
        behaviour, the ``PIPELINE_BACKEND_DISPATCH`` env var (normalized
        ``(raw or "claude").strip().lower() or "claude"``) and then
        "claude", never a crash. The registry is loaded fresh and the
        resolution is computed fresh on every call: nothing is memoized,
        nothing is written back to os.environ or the registry, so the
        result is a pure function of (registry, environ).

        Returns (provider, model, source). The model is reported for operator
        context only; it does not influence the pass/fail decision. When the
        registry/plan supply no model, the fallback mirrors the model each
        provider's dispatch chain actually uses: for claude,
        PIPELINE_DEFAULT_MODEL, else "sonnet" (pipeline/config.py's
        DEFAULT_MODEL); for every local-family provider (ollama, lmstudio,
        mlx, local, auto), PIPELINE_LOCAL_MODEL_DEFAULT, else
        PIPELINE_DEFAULT_MODEL, else the local backend's own default constant
        (app.backend_ollama's _LOCAL_DEFAULT_MODEL, imported lazily - see the
        module docstring's CRITICAL ORDERING rule).
        """
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from app.role_registry import RoleRegistryError, load_registry, resolve_role
        from pipeline.config_provenance import resolve_role_provenance

        def _model_fallback_for(provider: str) -> str:
            if provider == "claude":
                return os.environ.get("PIPELINE_DEFAULT_MODEL", "sonnet")
            if str(REPO_ROOT) not in sys.path:
                sys.path.insert(0, str(REPO_ROOT))
            from app.backend_ollama import _LOCAL_DEFAULT_MODEL

            return os.environ.get(
                "PIPELINE_LOCAL_MODEL_DEFAULT",
                os.environ.get("PIPELINE_DEFAULT_MODEL", _LOCAL_DEFAULT_MODEL),
            )

        def _env_or_default_source() -> str:
            if os.environ.get("PIPELINE_BACKEND_DISPATCH") is None:
                return (
                    "defaults (PIPELINE_BACKEND_DISPATCH unset -> claude, "
                    "matching pipeline/dispatch.py)"
                )
            return "env var PIPELINE_BACKEND_DISPATCH"

        def _provenance_source(provider_source: str | None) -> str:
            if provider_source == "model_registry.json":
                return "model_registry.json (roles.dispatch)"
            if provider_source == "env:PIPELINE_BACKEND_DISPATCH":
                return "env var PIPELINE_BACKEND_DISPATCH"
            if provider_source == "plan_role_config":
                return "plan role_config"
            return (
                "defaults (PIPELINE_BACKEND_DISPATCH unset -> claude, "
                "matching pipeline/dispatch.py)"
            )

        try:
            registry = load_registry()
        except RoleRegistryError:
            registry = {}
        try:
            provenance = resolve_role_provenance(
                "dispatch", registry=registry, environ=os.environ
            )
        except Exception:  # noqa: BLE001 - the label is diagnostic only
            provenance = {}
        known_provider = (provenance.get("provider") or "").strip().lower()

        try:
            resolution = resolve_role(
                "dispatch",
                registry=registry,
                model_fallback=lambda: _model_fallback_for(known_provider),
                environ=os.environ,
            )
        except RoleRegistryError as exc:
            # Fail open exactly like pipeline/dispatch.py: a fresh clone
            # (no roles.dispatch entry anywhere) or a poisoned entry (a
            # model named but not declared under its provider) degrades to
            # the pre-registry behaviour - the env var, then "claude" - with
            # no registry model. Stay loud about the poisoned shape: a load
            # failure is proof the entry exists and is unusable.
            print(
                "smoke: model_registry.json's roles.dispatch entry could not "
                f"resolve the dispatch role ({exc}); falling back to "
                "PIPELINE_BACKEND_DISPATCH, then claude.",
                file=sys.stderr,
            )
            backend = os.environ.get("PIPELINE_BACKEND_DISPATCH") or "claude"
            backend = backend.strip().lower() or "claude"
            return backend, _model_fallback_for(backend), _env_or_default_source()

        # Adjust model for local-family providers
        local_family = ("ollama", "lmstudio", "mlx", "local", "auto")
        provider = resolution.provider
        model = resolution.model
        if provider in local_family:
            model = os.environ.get("PIPELINE_LOCAL_MODEL_DEFAULT", model)
        return (
            provider,
            model,
            _provenance_source(provenance.get("provider_source")),
        )

    try:
        provider, model, source = (
            resolver if resolver is not None else _default_dispatch_resolver
        )()
    except Exception as exc:  # a broken resolver must fail closed, not traceback
        print(
            f"smoke: refusing to run: the dispatch backend could not be resolved: {exc!r} (value: {os.environ.get('PIPELINE_BACKEND_DISPATCH')!r})",
            file=sys.stderr,
        )
        print(
            "Fix: choose a provider explicitly - set "
            "PIPELINE_BACKEND_DISPATCH to one of the recognised providers "
            f"({', '.join(recognized)}); e.g. PIPELINE_BACKEND_DISPATCH=claude.",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    normalized_provider = provider.strip().lower()
    if normalized_provider == "" or normalized_provider not in recognized:
        _reject(provider, normalized_provider, source)

    # ANNOUNCE and PROCEED: one prominent line naming the resolved provider,
    # the resolved model and the source of the choice (the triple real
    # dispatch resolves), then continue - the smoke is provider-neutral.
    print(
        f"smoke: validating dispatch on {normalized_provider}/{model} "
        f"(source: {source})"
    )
    return normalized_provider, model, source


def run_smoke(tmp_root: Path | str, timeout_s: int = 1800) -> int:
    """Drive one story to `tests_passed` inside the scratch layout; returns exit code."""
    # 1. backend resolution guard FIRST (announces the resolved provider;
    # exits 2 only on an empty/unknown dispatch value). Nothing may be
    # created before it passes.
    provider, model, _source = _announce_dispatch_backend()

    # 2. claude CLI presence - ONLY when the resolved provider is claude: a
    # local-backend operator does not need the claude CLI, and requiring it
    # would be provider lock-in through a different door.
    if provider == "claude" and shutil.which("claude") is None:
        print("smoke: the `claude` CLI was not found on PATH.", file=sys.stderr)
        print(
            "The resolved dispatch provider is claude, which needs the claude "
            "CLI. Install it (https://claude.com/cli) and make sure it is on "
            "PATH, then re-run this smoke (or dispatch on a different "
            "provider via PIPELINE_BACKEND_DISPATCH).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # 3. scratch env (fail-closed abort if pipeline.paths resolves outside).
    layout = _prepare_scratch_env(tmp_root)
    plan_dir = Path(layout["PLAN_DIR"])
    target_repo = Path(layout["TARGET_REPO"])
    scratch_root = Path(tmp_root).resolve()
    print(
        f"smoke: scratch root {scratch_root} | target repo {target_repo} | "
        f"worktrees {layout['WORKTREE_ROOT']}"
    )

    # 4. lazy pipeline imports - only AFTER the scratch env vars are set.
    try:
        import pipeline.paths as pipeline_paths
        import pipeline.server as pipeline_server
    except ImportError as exc:
        print(
            f"smoke: cannot import the pipeline package ({exc}); run this "
            "script from a repo checkout with its dependencies installed.",
            file=sys.stderr,
        )
        raise SystemExit(5) from exc

    # Follow-up check: the guard passing at prep time must still hold at
    # write time - verify the bindings save_plan will actually use.
    for label, bound in (
        ("pipeline.paths.PLAN_DIR", pipeline_paths.PLAN_DIR),
        ("pipeline.server.PLAN_DIR", getattr(pipeline_server, "PLAN_DIR", None)),
    ):
        if bound is None:
            continue
        resolved = Path(bound).resolve()
        if not (resolved == scratch_root or scratch_root in resolved.parents):
            print(
                f"smoke: FAIL-CLOSED abort: {label}={resolved} is outside the "
                f"scratch root {scratch_root}; refusing to save the plan.",
                file=sys.stderr,
            )
            raise SystemExit(5)
    print(f"smoke: pipeline PLAN_DIR resolved under scratch: {pipeline_paths.PLAN_DIR}")

    # 5. save a minimal 1-epic/1-story plan (trivial change: append one line
    # to the scratch repo's README).
    plan = {
        "repo_root": str(target_repo),
        "epics": [
            {
                "summary": "Smoke: append one line to the scratch README",
                "stories": [
                    {
                        "key": STORY_KEY,
                        "summary": "Append a smoke-test line to README.md",
                        "description": (
                            "Getting-started smoke story: append exactly one "
                            "line to the scratch repo's README.md and commit it."
                        ),
                        "agent_instructions": (
                            "Append exactly one new line to README.md in the "
                            "repo root reading: 'Smoke line added by "
                            "scripts/smoke_getting_started.py.' Commit the "
                            "change with a short message. Do not modify any "
                            "other file. Success criteria: README.md contains "
                            "the smoke line; no other file changed."
                        ),
                        "persona": "software-engineer",
                        "risk": "low",
                        "dependencies": [],
                    }
                ],
            }
        ],
    }
    saved = pipeline_server.save_plan(PLAN_NAME, json.dumps(plan))
    if not saved.get("ok"):
        print(f"smoke: save_plan failed: {saved.get('error')}", file=sys.stderr)
        return 4
    ingested = pipeline_server.ingest_plan(PLAN_NAME)
    if not ingested.get("ok"):
        print(f"smoke: ingest_plan failed: {ingested.get('error')}", file=sys.stderr)
        return 4

    manifest_path = plan_dir / f"{PLAN_NAME}.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    stories = manifest.get("stories", {})
    story_key = STORY_KEY if STORY_KEY in stories else (
        next(iter(stories)) if len(stories) == 1 else None
    )
    if story_key is None:
        print(
            f"smoke: could not identify the ingested story key; manifest "
            f"stories: {sorted(stories)}",
            file=sys.stderr,
        )
        return 4

    # 6. dispatch, then bounded poll: advance_pipeline is the mutator that
    # drives progress (dispatch/grade/review/merge); the manifest read is the
    # pure status read (check_story_status grades finished agents as a side
    # effect - advance_pipeline already drives that internally, so reading
    # the manifest keeps the poll side-effect free).
    dispatched = pipeline_server.dispatch_story(PLAN_NAME, story_key)
    if isinstance(dispatched, dict) and dispatched.get("ok") is False:
        print(
            f"smoke: dispatch_story failed for {story_key}: "
            f"{dispatched.get('error')}",
            file=sys.stderr,
        )
        return 4

    deadline = time.monotonic() + timeout_s  # computed ONCE; monotonic never
    last_status = "todo"  # steps backwards, so the deadline always holds
    while True:
        try:
            pipeline_server.advance_pipeline(PLAN_NAME)
        except Exception as exc:  # noqa: BLE001 - transient tick errors must not kill the run
            print(f"smoke: advance_pipeline tick error (will retry): {exc}")
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            story = manifest.get("stories", {}).get(story_key, {})
            last_status = story.get("status", last_status)
        if last_status == "tests_passed":
            print(
                f"PASS: story {story_key} implemented, tests passed on "
                f"provider {provider} model {model} (plan {PLAN_NAME!r})"
            )
            print(f"  final status: {last_status}")
            print(f"  scratch plan dir: {plan_dir}")
            return 0
        if last_status in TERMINAL_FAILURE_STATUSES:
            print(f"FAIL: story {story_key} reached status {last_status!r}")
            print(f"  manifest: {manifest_path}")
            print(f"  dashboard.log: {plan_dir / 'dashboard.log'}")
            return 4
        now = time.monotonic()
        if now >= deadline:
            print(
                f"TIMEOUT after {timeout_s}s: story {story_key} last status "
                f"{last_status!r}"
            )
            print(f"  manifest: {manifest_path}")
            print(f"  dashboard.log: {plan_dir / 'dashboard.log'}")
            return 3
        time.sleep(min(POLL_INTERVAL_S, deadline - now))


def main(
    argv: list[str] | None = None,
    *,
    resolver: Callable[[], tuple[str, str, str]] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="smoke_getting_started",
        description=(
            "Scratch-PLAN_DIR getting-started smoke: run one story to "
            "tests_passed on the claude backend without touching "
            "~/.claude/plans."
        ),
    )
    parser.add_argument(
        "--check-preconditions",
        action="store_true",
        help="run the guards only (claude CLI on PATH, claude backend "
        "resolved) and exit; no plan dir is created",
    )
    parser.add_argument(
        "--timeout-s",
        type=int,
        default=1800,
        help="bounded poll budget in seconds (default 1800)",
    )
    parser.add_argument(
        "--tmp-root",
        type=Path,
        default=None,
        help="scratch root directory (default: a fresh temp dir)",
    )
    args = parser.parse_args(argv)

    if args.check_preconditions:
        # Backend guard first: it announces the resolved provider/model/
        # source and exits 2 only on an empty/unknown dispatch value - even
        # on a machine without the claude CLI (a bare CI runner), a declared
        # non-claude provider must pass here, not exit 1 for the missing CLI.
        provider, _model, _source = _announce_dispatch_backend(resolver=resolver)
        if provider == "claude" and shutil.which("claude") is None:
            print(
                "precondition FAIL: the resolved dispatch provider is claude "
                "but the `claude` CLI was not found on PATH; install it "
                "(https://claude.com/cli) or add it to PATH (or dispatch on "
                "a different provider via PIPELINE_BACKEND_DISPATCH)."
            )
            return 1
        print(
            f"precondition check PASS: dispatch provider {provider} "
            "validated"
        )
        return 0

    tmp_root = (
        args.tmp_root
        if args.tmp_root is not None
        else Path(tempfile.mkdtemp(prefix="smoke-getting-started-"))
    )
    return run_smoke(tmp_root, timeout_s=args.timeout_s)


if __name__ == "__main__":
    raise SystemExit(main())