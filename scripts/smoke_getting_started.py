"""Scratch-PLAN_DIR getting-started smoke: run ONE story to merge on claude.

This is the operator-facing "does a fresh checkout actually work?" driver.
It builds a throwaway scratch layout, saves + ingests a minimal 1-epic /
1-story plan against a scratch target git repo, dispatches the story on the
``claude`` backend, and polls until the story is merged - all without ever
writing into the operator's real ~/.claude/plans.

Scratch layout (everything under one tmp root):

    <tmp_root>/plans/      PLAN_DIR  (plan JSON + manifest live here)
    <tmp_root>/worktrees/  WORKTREE_ROOT (agent worktrees)
    <tmp_root>/repo/       scratch target git repo (one committed README)

CRITICAL ORDERING: pipeline/paths.py reads PLAN_DIR/WORKTREE_ROOT from
os.environ AT IMPORT TIME, so this script sets those env vars BEFORE the
first pipeline.* import (all pipeline imports are lazy, inside functions,
after the env writes). app.role_registry is imported the same way - lazily,
inside the backend guard's advisory registry note, never at module scope.
A fail-closed guard then re-verifies the resolved
``pipeline.paths.PLAN_DIR`` module attribute (not the env var - a stale
cached module would make an env-only check lie) is inside the scratch root
and aborts nonzero otherwise.

Exit codes:
    0  PASS - story merged (status "done"); story key + PR URL printed
    1  the ``claude`` CLI is not on PATH (install hint printed)
    2  PIPELINE_BACKEND_DISPATCH resolves to a local-family/unknown backend.
       The guard mirrors the resolution real dispatch actually performs
       (pipeline/dispatch.py, pipeline/advance.py, pipeline/preflight.py):
       the raw env var, .strip().lower(), default "claude" - the registry's
       roles.dispatch entry never gates dispatch (it only feeds the
       dashboard display and decompose-time sizing), so it is reported
       as an advisory note only, never as the dispatch decision.
    3  the bounded poll timed out (default 30 min, checked every 15 s)
    4  the story reached a terminal failure status (failed/parked)
    5  fail-closed abort: resolved pipeline paths landed outside the scratch

Usage:
    python scripts/smoke_getting_started.py [--check-preconditions]
        [--timeout-s N] [--tmp-root DIR]

``--check-preconditions`` runs only the two guards (claude CLI on PATH,
claude backend resolved) and exits - it creates nothing.
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


class _DispatchResolutionError(Exception):
    """The advisory registry note could not be produced.

    Raised by the guard's advisory registry note when app.role_registry
    cannot be imported or rejects the registry contents. Purely diagnostic:
    the note never gates the guard's pass/fail decision (exit codes come
    from the env-var resolution alone), so any failure here is swallowed
    after printing a one-line warning.
    """


def _require_claude_backend(
    value: str | None = None,
    *,
    resolver: Callable[[], tuple[str, str, str]] | None = None,
) -> None:
    """Fail closed unless the dispatch backend resolves to ``claude``.

    Two modes:

    *value* handed in as a string (pure, no I/O): the legacy env-value
    check. Normalization mirrors the dispatch chain exactly
    (``.strip().lower()`` - see pipeline/dispatch.py and pipeline/advance.py).
    Every local-family value (auto/ollama/lmstudio/mlx/local), empty strings
    and unknown values exit 2: the smoke must never silently depend on a
    local backend. This mode never mutates os.environ.

    *value* left unset (what ``main()``/``run_smoke()`` use): the guard
    resolves the backend the way real dispatch actually does - via
    *resolver* (a callable returning ``(provider, model, source)``; the
    default resolver mirrors pipeline/preflight.py's check c exactly: the
    raw ``PIPELINE_BACKEND_DISPATCH`` env var, ``(raw or "claude")
    .strip().lower() or "claude"``). Real dispatch NEVER consults
    app.role_registry for provider selection - the registry's
    roles.dispatch entry only feeds the dashboard display and decompose-time
    sizing - so the registry is never part of the resolution; on a PASS it
    is reported as a separate, clearly-labeled, best-effort ADVISORY note
    (never gating, never affecting the exit code). Exit 2 when the resolved
    provider is anything but ``claude`` (local family, unknown, empty); the
    printed message names the resolved provider AND where the choice came
    from so the operator can change it. This guard never mutates os.environ.
    """
    if value is not None:
        raw = value
        normalized = raw.strip().lower()
        if normalized == "claude":
            print("smoke: backend guard OK: PIPELINE_BACKEND_DISPATCH resolves to claude")
            return
        print(
            f"smoke: refusing to run: PIPELINE_BACKEND_DISPATCH={raw!r} "
            f"(normalized {normalized!r}) is not the claude backend.",
            file=sys.stderr,
        )
        print(
            "This smoke must never silently depend on a local backend "
            "(ollama/lmstudio/mlx/local/auto) or an unknown value. Fix: unset "
            "PIPELINE_BACKEND_DISPATCH or set PIPELINE_BACKEND_DISPATCH=claude, "
            "and make sure the `claude` CLI is installed and on PATH.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    def _default_dispatch_resolver() -> tuple[str, str, str]:
        """Resolve the dispatch backend the way real dispatch actually does.

        Mirrors pipeline/preflight.py's check c (and pipeline/dispatch.py /
        pipeline/advance.py's env_backend read) EXACTLY: the raw
        ``PIPELINE_BACKEND_DISPATCH`` env var, ``(raw or "claude")
        .strip().lower() or "claude"``. Deliberately NO registry
        consultation: app.role_registry.resolve_role("dispatch") is used in
        production only for the dashboard display and decompose-time sizing,
        never to pick the backend a story dispatches on - honoring the
        registry here would make the smoke disagree with the system it
        guards (e.g. refuse to run when roles.dispatch="local" is set for
        those unrelated use cases while real dispatch correctly uses claude).

        Returns (provider, model, source). The model is the dispatch
        chain's own bottom default (PIPELINE_DEFAULT_MODEL, else "sonnet" -
        pipeline/config.py's DEFAULT_MODEL), reported for operator context
        only; it does not influence the pass/fail decision.
        """
        raw_backend = os.environ.get("PIPELINE_BACKEND_DISPATCH", "claude")
        backend = (raw_backend or "claude").strip().lower() or "claude"
        model = os.environ.get("PIPELINE_DEFAULT_MODEL", "sonnet")
        if os.environ.get("PIPELINE_BACKEND_DISPATCH") is None:
            source = (
                "defaults (PIPELINE_BACKEND_DISPATCH unset -> claude, "
                "matching pipeline/dispatch.py)"
            )
        else:
            source = "env var PIPELINE_BACKEND_DISPATCH"
        return backend, model, source

    def _advise_if_registry_differs(provider: str) -> None:
        """Best-effort ADVISORY note about the registry's roles.dispatch entry.

        The registry entry never gates dispatch (see
        _default_dispatch_resolver), but if it names a DIFFERENT provider
        than the one dispatch will actually use, the operator deserves a
        one-line heads-up that the two signals have drifted apart - the
        entry only feeds the dashboard display and decompose-time sizing.
        Purely informational: swallows every failure (missing module,
        malformed registry) after a one-line warning and NEVER exits
        nonzero. CRITICAL ORDERING: app.role_registry is imported HERE,
        not at module scope (see the module docstring).
        """
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        try:
            from app.role_registry import load_registry

            role_cfg = load_registry().get("roles", {}).get("dispatch", {})
            registry_provider = (role_cfg.get("provider") or "").strip().lower()
            if not registry_provider or registry_provider == provider:
                return
            print(
                f"smoke: note (advisory, does not affect this check): "
                f"model_registry.json's roles.dispatch entry names provider "
                f"{registry_provider!r}, but dispatch actually resolves via "
                f"PIPELINE_BACKEND_DISPATCH to {provider!r} - the registry "
                "entry only feeds the dashboard display and decompose-time "
                "sizing, never real dispatch."
            )
        except Exception as exc:  # noqa: BLE001 - advisory only, never gating
            print(
                "smoke: note (advisory registry check skipped): "
                f"{exc}",
                file=sys.stderr,
            )

    try:
        provider, model, source = (
            resolver if resolver is not None else _default_dispatch_resolver
        )()
    except _DispatchResolutionError as exc:
        print(
            "smoke: refusing to run: the dispatch backend could not be "
            f"resolved: {exc}",
            file=sys.stderr,
        )
        print(
            "Fix: choose a provider explicitly - set "
            "PIPELINE_BACKEND_DISPATCH=claude (and make sure the `claude` CLI "
            "is installed and on PATH).",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    if provider.strip().lower() == "claude":
        print(
            f"smoke: backend guard OK: dispatch resolves to claude "
            f"(model {model!r}) via {source}"
        )
        _advise_if_registry_differs(provider.strip().lower())
        return
    print(
        f"smoke: refusing to run: PIPELINE_BACKEND_DISPATCH resolves to "
        f"{provider!r} (model {model!r}) via {source} - not the claude "
        "backend.",
        file=sys.stderr,
    )
    print(
        "This smoke must never silently depend on a local backend "
        "(ollama/lmstudio/mlx/local/auto) or an unknown value. Fix: unset "
        "PIPELINE_BACKEND_DISPATCH or set PIPELINE_BACKEND_DISPATCH=claude, "
        "and make sure the `claude` CLI is installed and on PATH.",
        file=sys.stderr,
    )
    raise SystemExit(2)


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


def build_smoke_plan(repo_root: str) -> dict:
    """Build the minimal 1-epic/1-story smoke plan for the scratch repo.

    Extracted from run_smoke() so the plan's shape can be asserted without
    running the whole pipeline. Per .claude/rules/pipeline-story-schema.md,
    the acceptance field is an array of {path, source} file fixtures, not
    criteria strings - the success criteria live in agent_instructions instead.
    """
    return {
        "repo_root": str(repo_root),
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


def run_smoke(tmp_root: Path | str, timeout_s: int = 1800) -> int:
    """Drive one story to merge inside the scratch layout; returns exit code."""
    # 1. claude CLI presence FIRST - nothing may be created before it passes.
    if shutil.which("claude") is None:
        print("smoke: the `claude` CLI was not found on PATH.", file=sys.stderr)
        print(
            "Install the claude CLI (https://claude.com/cli) and make sure it "
            "is on PATH, then re-run this smoke.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # 2. backend resolution guard (exit 2 on any local-family value).
    _require_claude_backend()

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
    plan = build_smoke_plan(str(target_repo))
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
    pr_url = None
    while True:
        try:
            pipeline_server.advance_pipeline(PLAN_NAME)
        except Exception as exc:  # noqa: BLE001 - transient tick errors must not kill the run
            print(f"smoke: advance_pipeline tick error (will retry): {exc}")
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            story = manifest.get("stories", {}).get(story_key, {})
            last_status = story.get("status", last_status)
            pr_url = story.get("pr_url") or pr_url
        if last_status == "done":
            print(f"PASS: story {story_key} merged (plan {PLAN_NAME!r})")
            print(f"  PR URL: {pr_url or '(none recorded)'}")
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
            "Scratch-PLAN_DIR getting-started smoke: run one story to merge "
            "on the claude backend without touching ~/.claude/plans."
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
        # Backend guard first: under a local-family/unknown dispatch backend
        # the script must exit 2 even on a machine without the claude CLI
        # (a bare CI runner), not 1 for the missing CLI.
        _require_claude_backend(resolver=resolver)  # exits 2 on a
        # local-family/unknown/unresolvable dispatch backend
        if shutil.which("claude") is None:
            print(
                "precondition FAIL: the `claude` CLI was not found on PATH; "
                "install it (https://claude.com/cli) or add it to PATH."
            )
            return 1
        print("precondition check PASS: claude CLI on PATH, claude backend resolved")
        return 0

    tmp_root = (
        args.tmp_root
        if args.tmp_root is not None
        else Path(tempfile.mkdtemp(prefix="smoke-getting-started-"))
    )
    return run_smoke(tmp_root, timeout_s=args.timeout_s)


if __name__ == "__main__":
    raise SystemExit(main())