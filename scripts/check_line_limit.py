"""Two-tier 1000-line-per-file gate (CLAUDE.md, Architecture).

Why two tiers
-------------
A gate that fails on every over-limit text file would be red the moment it
landed: most over-limit files in this repo are docs, CSS, .mjs and tests, and
legitimate doc growth (REFERENCE.md) must never turn CI red. So:

  BLOCKING (exit 1) -- production Python only: repo-root *.py, pipeline/**,
    app/**, scripts/**. This is where every motivating incident came from
    (pipeline/story_status.py and pipeline/dispatch.py both drifted back over
    the line within a week of being split on 2026-09-23).
  WARN-ONLY (never fails) -- tests, docs, .css, .mjs. Printed with their count
    so the drift stays visible.

Counting method
---------------
``sum(1 for _ in fh)`` -- the same method as pipeline/ingest.py's existing
``_SIZING_MAX_FILE_LINES`` check. This matters: it exceeds ``wc -l`` by one for
any file whose last line has no trailing newline (pipeline/worktree_patch.py is
1004 by this method, 1003 by ``wc -l``). Every ceiling below is recorded with
this method; mixing in ``wc -l`` would make the gate red on its first CI run.

Allowlists
----------
Both are CEILINGS (fixed high-water marks), not exemptions. An allowlisted
production file that grows past its ceiling fails, and an allowlisted
production file that no longer exists or has come under the limit fails with a
"remove this entry" message -- otherwise the list becomes a permanent
exemption list. Each split story retires its own entry.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_LIMIT = 1000

# ALLOWLIST of known-text extensions. Never a denylist of binary extensions:
# a denylist fails open -- an unforeseen binary extension would be read as text
# and produce meaningless violations (docs/screenshots/demo.gif has 1463
# "lines" and is pure noise; an allowlist excludes it structurally).
TEXT_EXTENSIONS = frozenset({".py", ".md", ".css", ".mjs", ".js"})

EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

# Production scopes. A repo-root *.py is production too (no "/" in the path).
BLOCKING_PREFIXES = ("pipeline/", "app/", "scripts/")

# Production files over the limit today, with the count recorded 2026-09-29
# using the counting method above. Each entry is retired by the split story
# that brings the file under the limit.
_BLOCKING_ALLOWLIST = {
    "scripts/local_agent.py": 1084,
    "scripts/local_agent_oracle.py": 1012,
    "pipeline/story_status.py": 1012,
}

# Non-production text files over the limit today. Growth past the ceiling is
# reported as a warning, never as a failure.
_WARN_ALLOWLIST = {
    "docs/plans/GUIDED_DECOMPOSITION_PLAN.md": 1725,
    "REFERENCE.md": 1791,
    "tests/unit/test_comms_sse_stream.mjs": 1439,
    "static/style.css": 1292,
    "tests/unit/test_pipeline_mcp_server_reverify_build.py": 1252,
    "tests/unit/test_standalone_setup_script.py": 1099,
    "tests/unit/test_patch_ui_wiring.mjs": 1086,
    "tests/unit/test_wedge_scan.py": 1051,
    "tests/unit/test_backend_resource_status.py": 1043,
}


def count_lines(path: Path) -> int:
    """Line count, matching pipeline/ingest.py:140 exactly.

    ``errors="replace"`` so a stray non-UTF-8 byte cannot crash the gate.
    """
    with open(path, "r", errors="replace") as fh:
        return sum(1 for _ in fh)


def is_blocking(rel: str) -> bool:
    """True for production Python: repo-root *.py, pipeline/**, app/**,
    scripts/**. Everything else is warn-only."""
    if not rel.endswith(".py"):
        return False
    if "/" not in rel:
        return True
    return rel.startswith(BLOCKING_PREFIXES)


def _iter_tracked(root: Path):
    """Yield repo-relative paths of tracked files.

    Prefers ``git ls-files`` (tracked files only, so gitignored paths are
    excluded by construction). Falls back to a pruned walk for trees that are
    not git repos -- e.g. the tmp_path fixtures in the tests.
    """
    if (root / ".git").exists():
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            for rel in proc.stdout.split("\0"):
                if rel:
                    yield rel
            return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for name in filenames:
            full = Path(dirpath) / name
            yield full.relative_to(root).as_posix()


def check(root: Path, limit: int = DEFAULT_LIMIT) -> int:
    """Run the gate. Returns the process exit code (0 clean, 1 blocking)."""
    blocking_violations: list[tuple[str, int, int | None]] = []
    warn_violations: list[tuple[str, int, int | None]] = []
    stale_entries: list[tuple[str, int | None]] = []
    warn_only_over_limit = 0

    for rel in sorted(set(_iter_tracked(root))):
        full = root / rel
        if not full.is_file():
            continue
        if full.suffix not in TEXT_EXTENSIONS:
            continue
        count = count_lines(full)
        if count <= limit:
            continue
        if is_blocking(rel):
            ceiling = _BLOCKING_ALLOWLIST.get(rel)
            if ceiling is not None and count <= ceiling:
                continue
            blocking_violations.append((rel, count, ceiling))
        else:
            warn_only_over_limit += 1
            ceiling = _WARN_ALLOWLIST.get(rel)
            if ceiling is not None and count <= ceiling:
                continue
            warn_violations.append((rel, count, ceiling))

    # Stale-entry check: an allowlisted production file that no longer exists,
    # or that has come under the limit, must fail so the entry gets removed.
    # Without this the allowlist is a permanent exemption list.
    for rel in _BLOCKING_ALLOWLIST:
        full = root / rel
        if not full.is_file():
            stale_entries.append((rel, None))
            continue
        count = count_lines(full)
        if count <= limit:
            stale_entries.append((rel, count))

    for rel, count, ceiling in blocking_violations:
        if ceiling is None:
            print(f"{rel}: {count} lines (limit {limit})")
        else:
            print(f"{rel}: {count} lines (limit {limit}, ceiling {ceiling})")

    for rel, count in stale_entries:
        if count is None:
            print(
                f"{rel}: allowlisted but no longer exists (limit {limit}) "
                "\u2014 remove this entry from the BLOCKING allowlist"
            )
        else:
            print(
                f"{rel}: allowlisted but now {count} lines (limit {limit}) "
                "\u2014 remove this entry from the BLOCKING allowlist"
            )

    for rel, count, ceiling in warn_violations:
        if ceiling is None:
            print(f"warn-only: {rel}: {count} lines (limit {limit})")
        else:
            print(f"warn-only: {rel}: {count} lines (limit {limit}, ceiling {ceiling})")

    if blocking_violations or stale_entries:
        print(
            f"FAIL: {len(blocking_violations)} blocking violation(s), "
            f"{len(stale_entries)} stale allowlist entry(ies)"
        )
        return 1

    print(f"OK: no blocking violations ({warn_only_over_limit} warn-only file(s) over limit)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Two-tier 1000-line-per-file gate (blocking on production "
        "Python, warn-only elsewhere)."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"line limit per file (default {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root to scan (default: this script's repo)",
    )
    args = parser.parse_args(argv)
    return check(args.root.resolve(), args.limit)


if __name__ == "__main__":
    sys.exit(main())
