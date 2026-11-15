"""Stale-checkout notice for the scheduler daemon (story SSA-1).

Moved out of ``pipeline/scheduler_daemon.py`` without behaviour change. The
``SchedulerDaemon._notify_if_checkout_stale`` method delegates here.

The logger keeps the ``pipeline.scheduler_daemon`` name so the daemon's log
channel is unchanged by where the code lives.
"""
import logging
import os
import subprocess

from pipeline import manifest_source

logger = logging.getLogger("pipeline.scheduler_daemon")

# Upstream revisions already announced as stale (story SSA-1). Module-level so
# the dedup survives across the daemon's many health writes; never reassigned.
STALE_CHECKOUT_NOTIFIED: set[str] = set()


def notify_if_checkout_stale(health: dict) -> None:
    """Notify once per upstream revision when the daemon is running stale code.

    A merged orchestration change is invisible to this process until it is
    restarted. ``checkout_behind_origin`` — computed by
    ``config_fingerprint()``, deliberately NOT by ``health()``, whose key
    set is pinned — says the checkout trails upstream; this function adds the
    two facts that decide whether that matters: the upstream revision (for
    one-notice-per-revision dedup) and whether the missing commits touch
    ``pipeline/`` or ``app/``. Doc-only and test-only merges stay silent.

    *health* is the config fingerprint dict, not ``health()``.
    """
    from pipeline import paths

    behind = health.get("checkout_behind_origin")
    if not behind:
        return
    checkout = os.path.dirname(os.path.dirname(os.path.abspath(paths.__file__)))

    def _git(*args: str):
        return subprocess.run(
            ["git", *args], cwd=checkout, check=False,
            capture_output=True, text=True, timeout=10,
        )

    # Resolve the upstream revision first: once announced, the diff probe
    # is pure waste, and this runs on every health write.
    upstream = _git("rev-parse", "@{upstream}")
    if upstream.returncode != 0:
        return
    upstream_sha = upstream.stdout.strip()
    if not upstream_sha or upstream_sha in STALE_CHECKOUT_NOTIFIED:
        return
    diff = _git("diff", "--name-only", "HEAD..@{upstream}")
    if diff.returncode != 0:
        return
    code_paths = [
        name for name in diff.stdout.splitlines()
        if name.startswith(("pipeline/", "app/"))
    ]
    if not code_paths:
        return
    try:
        names = manifest_source.read_plan_names(fallback_on_error=True)
    except Exception:  # a notice must never break the daemon
        logger.debug("plan listing for the stale-checkout notice failed", exc_info=True)
        return
    if not names:
        return
    plan_name = names[0]
    from .persistence import _notify_user

    _notify_user(
        plan_name,
        f"The running scheduler is {behind} commit(s) behind its upstream "
        f"and the missing commits change code (e.g. {code_paths[0]}). The "
        "daemon does not hot-reload: run scripts/reload_pipeline_daemon.sh "
        "to activate the merged code.",
        event="scheduler_reload_required",
        dedup_key=f"scheduler_stale:{upstream_sha}",
    )
    STALE_CHECKOUT_NOTIFIED.add(upstream_sha)
