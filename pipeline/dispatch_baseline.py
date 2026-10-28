import fcntl
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

from .build_detect import detect_test_command

logger = logging.getLogger(__name__)

_BASELINE_TEST_TIMEOUT_S = 900

# A cached snapshot is only trusted while it is recent: an environment fix
# (a missing dependency installed, a flaky upstream patch) should reach the
# next dispatch without an operator clearing the cache by hand.
_BASELINE_CACHE_TTL_S = 14 * 24 * 60 * 60
_BASELINE_CACHE_DIRNAME = "baseline_cache"
_BASELINE_CACHE_KEY_CHARS = 32
_GIT_HEAD_TIMEOUT_S = 15


def _baseline_test_env() -> dict[str, str]:
    """The dispatch process's env minus the operational overrides that
    false-fail a suite in a repo that vendors the pipeline's own tests.

    Mirrors the grading gate's env (story_status.py's test_env: the same
    PIPELINE_*/LOCAL_AGENT_*/REPO_ROOT strips) so the baseline snapshot and
    the real gate agree on what "failing" means. The server carries
    PIPELINE_* config (pause/resume thresholds, backend dispatch, model
    defaults) that overrides the defaults the suite asserts against, and
    REPO_ROOT is a per-plan sentinel (/nonexistent-...) that isn't a
    developer default - either one surviving into the run false-fails the
    suite for every story in the pipeline repo itself.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("PIPELINE_")
        and not k.startswith("LOCAL_AGENT_")
        and k != "REPO_ROOT"
    }


def _cache_dir() -> Path:
    """The per-install snapshot cache directory, resolved at call time.

    ``pipeline.server.PLAN_DIR`` is the single surface tests and operators
    patch to relocate plan storage, so the cache follows it rather than
    binding the path at import.
    """
    import pipeline.server

    return Path(pipeline.server.PLAN_DIR) / _BASELINE_CACHE_DIRNAME


def _git_head(worktree_path: Path) -> str | None:
    """The worktree's commit sha, or None when it is not a readable repo."""
    try:
        r = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_HEAD_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None


def _detected_key_parts(worktree_path: Path) -> tuple[list[str], str] | None:
    """The (command, worktree-relative test dir) half of the cache key.

    ``detect_test_command`` answers with a ``(test_dir, command)`` pair, but
    it is patched in tests with a plain mapping; both shapes are accepted so
    the key stays a faithful function of what will actually run. Never
    raises: an undetectable command simply means "no cache", not "no
    snapshot".
    """
    try:
        detected = detect_test_command(worktree_path)
    except Exception:  # noqa: BLE001 - key derivation must never block a dispatch
        return None
    if not detected:
        return None
    if isinstance(detected, dict):
        command = detected.get("command") or []
        test_dir = detected.get("test_dir", worktree_path)
    else:
        try:
            test_dir, command = detected
        except (TypeError, ValueError):
            return None
    try:
        return [str(part) for part in command], _relative_test_dir(
            worktree_path, test_dir
        )
    except TypeError:
        return None


def _relative_test_dir(worktree_path: Path, test_dir: object) -> str:
    """``test_dir`` expressed relative to the worktree, else verbatim.

    A relative form keeps the key stable across the per-dispatch worktree
    paths that all point at the same commit.
    """
    try:
        return (
            Path(str(test_dir)).resolve().relative_to(worktree_path.resolve()).as_posix()
        )
    except (OSError, ValueError):
        return str(test_dir)


def _read_cache(path: Path) -> dict | None:
    """The cached snapshot, or None when absent/unreadable/unparseable."""
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        cached = json.loads(raw)
    except ValueError:
        return None
    return cached if isinstance(cached, dict) else None


def _write_cache(path: Path, result: dict) -> None:
    """Serialise ``result`` to ``path`` atomically with owner-only mode.

    Raises ``TypeError``/``ValueError`` when the result cannot be serialised
    and ``OSError`` when it cannot be stored; both are the caller's cue to
    fall back to the in-memory result rather than fail the dispatch.
    """
    data = json.dumps(result, sort_keys=True, default=str).encode()
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-baseline-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _prune_cache(cache_dir: Path, keep: Path) -> None:
    """Drop cache entries older than the TTL. One listing; never raises."""
    cutoff = time.time() - _BASELINE_CACHE_TTL_S
    try:
        for entry in cache_dir.glob("*.json"):
            if entry == keep:
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _run_baseline_test_snapshot_uncached(worktree_path: Path) -> dict | None:
    """Run the detected test command once against a freshly created
    worktree, BEFORE the dispatched agent (or the test-author/planner
    phases) touch anything, and return its result in the same shape
    story_status.py's last_test_check already uses. Used to tell the
    agent up front which failures (if any) predate its own changes, so it
    doesn't spend budget investigating or "fixing" something it didn't
    cause. Observed live 2026-09-17: 4 separate stories independently
    rediscovered the same pre-existing failure pattern from scratch.
    Never raises; returns None on any failure (missing worktree, no
    detectable test command that can run, etc.) -- this is purely
    informational and must never block or slow down a normal dispatch on
    a repo where nothing is wrong."""
    from .build_detect import detect_test_command, failed_node_ids
    if not worktree_path.is_dir():
        return None
    test_dir, test_cmd = detect_test_command(worktree_path)
    # detect_test_command's documented no-op for a repo with no build system
    # at all (`[sys.executable, "-c", "pass"]`): there is no test suite to
    # snapshot, so spawning it would be a pointless extra subprocess on every
    # fresh dispatch of such a repo (and a spurious non-git spawn for callers
    # that assert on the subprocesses a dispatch makes).
    if len(test_cmd) == 3 and test_cmd[1:] == ["-c", "pass"]:
        return None
    try:
        r = subprocess.run(
            test_cmd, check=False, cwd=test_dir, capture_output=True, text=True,
            env=_baseline_test_env(), timeout=_BASELINE_TEST_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # Fail open: a suite that outruns the bound tells us nothing about
        # whether it was ALREADY failing, and the snapshot must never hold the
        # dispatch path open indefinitely (the docstring's "never block or
        # slow down a normal dispatch").
        return None
    return {
        "cmd": test_cmd,
        "returncode": r.returncode,
        "stdout_tail": (r.stdout or "")[-1000:],
        "failed_node_ids": failed_node_ids(r.stdout or ""),
        "stderr_tail": (r.stderr or "")[-1000:],
    }


def _run_baseline_test_snapshot(worktree_path: Path) -> dict | None:
    """``_run_baseline_test_snapshot_uncached``, memoised per commit.

    Fresh worktrees start at the default branch's head, so concurrent and
    consecutive dispatches of the same repository usually snapshot the same
    commit; the full suite (bounded at 900 s) is therefore run at most once
    per (commit, detected command, test dir) per cache TTL window. The key
    covers the command and the worktree-relative test dir so a change to
    either is a miss, and a per-key ``flock`` serialises concurrent
    dispatches so exactly one of them runs the suite while the others reuse
    its result. Only successful, non-None snapshots are stored: "no suite /
    timed out" is not evidence about the next call. The return value is
    exactly what the uncached run would have returned - the cache adds no
    keys and never raises.
    """
    if not worktree_path.is_dir():
        return None
    sha = _git_head(worktree_path)
    if sha is None:
        return _run_baseline_test_snapshot_uncached(worktree_path)
    parts = _detected_key_parts(worktree_path)
    if parts is None:
        return _run_baseline_test_snapshot_uncached(worktree_path)
    command, test_dir = parts
    payload = {"command": command, "sha": sha, "test_dir": test_dir}
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:_BASELINE_CACHE_KEY_CHARS]
    cache_dir = _cache_dir()
    path = cache_dir / f"{digest}.json"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(cache_dir, 0o700)
        fd = os.open(cache_dir / f"{digest}.json.lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        # An unusable cache directory must degrade to the plain snapshot.
        return _run_baseline_test_snapshot_uncached(worktree_path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        cached = _read_cache(path)
        if cached is not None:
            logger.info("dispatch baseline cache hit sha=%s hit=true", sha[:12])
            return cached
        result = _run_baseline_test_snapshot_uncached(worktree_path)
        if result is not None:
            try:
                _write_cache(path, result)
            except (OSError, TypeError, ValueError):
                return result
            _prune_cache(cache_dir, keep=path)
        return result
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
