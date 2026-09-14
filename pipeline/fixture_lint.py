"""Lint acceptance fixture sources at ingest time (OPSA-8).

Plan authors hand-write acceptance fixture sources; a typo or a lint
violation in one otherwise surfaces only after dispatch, when the executor
runs the fixture and fails. This module lints the *source text* of a ``.py``
acceptance entry with the repo's pinned ruff (``ruff==0.16.5`` — see
``requirements-dev.txt``) before the plan is ingested, so a bad fixture
rejects the ingest instead of burning an executor run.

SOURCE-ONLY: the fixture is never imported or executed — the source is
materialized into a temp file, linted as text, and the temp dir is removed.
``pipeline/patch_acceptance`` (OPSA-7) imports the same helper for its
patched-fixture validation, so keep this module free of server imports.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# Pinned EXACT (requirements-dev.txt): a different ruff version flags
# different rules, so "clean" would silently mean something else here than it
# does in CI's lint job. Fail closed on any mismatch.
RUFF_PINNED_VERSION = "0.16.5"

# Targeted rule selection, NOT ruff's defaults: the gate exists to catch
# plan-author mistakes that make a fixture fail at dispatch time — syntax
# errors (E9), assigned-but-never-used locals (F841) and unused unpacked
# variables (RUF059). Defaults would also flag F401 (unused import), but
# acceptance fixtures legitimately `import pytest` for fixtures/markers
# resolved at runtime, and the fixture sources embedded in the existing
# ingest tests do exactly that; flagging F401 here would reject plans that
# are fine. F821 (undefined name) is likewise omitted: fixture sources are
# often fragments that reference the unit under test without its import
# block (see the isolation-only-warning ingest test), so an undefined name
# in the source text is not proof of a broken fixture. Style rules (I001
# etc.) are out of scope: pyproject.toml already exempts acceptance fixtures
# from import-sort for exactly this reason.
LINT_SELECT = ["E9", "F841", "RUF059"]

# A lint run over a single fixture file is sub-second work; a hang means the
# binary is wedged, not that the fixture is big. Fail closed on timeout.
RUFF_TIMEOUT_SECONDS = 60

logger = logging.getLogger("pipeline")


class FixtureLintError(RuntimeError):
    """Ruff could not run at all (missing binary, crash, timeout, version mismatch).

    Callers must treat this as a rejection, not a pass: a lint gate that
    fails open is advisory, and this one exists to catch plan-author mistakes.
    """


def _find_ruff_binary(repo_root: Path) -> str:
    """Locate the ruff binary, preferring the repo's own venv.

    Raises FixtureLintError when no usable binary exists — fail closed.
    """
    candidates = [
        repo_root / ".venv" / "bin" / "ruff",
        repo_root / "venv" / "bin" / "ruff",
        repo_root / ".venv" / "Scripts" / "ruff.exe",
        repo_root / "venv" / "Scripts" / "ruff.exe",
    ]
    for candidate in candidates:
        if candidate.is_file() and os_access(candidate):
            return str(candidate)
    found = shutil.which("ruff")
    if found:
        return found
    raise FixtureLintError(
        "ruff binary not found (looked in the repo venv and PATH); "
        "cannot lint acceptance fixture source"
    )


def os_access(path: Path) -> bool:
    """Executable check that also works when the mode bits are unreadable."""
    import os

    return os.access(path, os.X_OK)


def _ruff_version(ruff_bin: str) -> str:
    try:
        proc = subprocess.run(
            [ruff_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=RUFF_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FixtureLintError(f"ruff --version failed to run: {exc}") from exc
    if proc.returncode != 0:
        raise FixtureLintError(
            f"ruff --version exited {proc.returncode}: {proc.stderr.strip()}"
        )
    match = re.search(r"(\d+\.\d+\.\d+)", proc.stdout)
    if not match:
        raise FixtureLintError(
            f"could not parse ruff version from: {proc.stdout.strip()!r}"
        )
    return match.group(1)


def lint_acceptance_source(source: str, repo_root: Path) -> list[str]:
    """Lint one acceptance fixture *source string*; return violation lines.

    Empty list = clean. Raises FixtureLintError when ruff cannot run at all
    (missing binary, crash, timeout, wrong version) — callers must reject.
    """
    if not isinstance(source, str):
        raise FixtureLintError("acceptance fixture source must be a string")

    ruff_bin = _find_ruff_binary(Path(repo_root))
    version = _ruff_version(ruff_bin)
    if version != RUFF_PINNED_VERSION:
        raise FixtureLintError(
            f"ruff version mismatch: found {version}, repo pins "
            f"{RUFF_PINNED_VERSION} (requirements-dev.txt); a different "
            "version changes what 'clean' means"
        )

    # Materialize the source into a temp file under a throwaway dir: ruff
    # lints files, not stdin-with-config, and cwd=repo_root makes ruff pick
    # up the repo's own ruff config (pyproject.toml / ruff.toml).
    tmp_dir = Path(tempfile.mkdtemp(prefix="opsa-fixture-lint-"))
    try:
        tmp_file = tmp_dir / "acceptance_fixture.py"
        tmp_file.write_text(source, encoding="utf-8")
        try:
            proc = subprocess.run(
                [
                    ruff_bin,
                    "check",
                    "--select",
                    ",".join(LINT_SELECT),
                    "--output-format",
                    "concise",
                    str(tmp_file),
                ],
                capture_output=True,
                text=True,
                timeout=RUFF_TIMEOUT_SECONDS,
                cwd=str(repo_root),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FixtureLintError(f"ruff check failed to run: {exc}") from exc
        if proc.returncode not in (0, 1):
            # 0 = clean, 1 = violations found; anything else is a crash.
            raise FixtureLintError(
                f"ruff check exited {proc.returncode}: {proc.stderr.strip()}"
            )
        violations = [
            line.strip()
            for line in proc.stdout.splitlines()
            if line.strip() and line.strip() != "All checks passed!"
        ]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if violations:
        logger.info(
            "fixture lint: %d violation(s) in acceptance source", len(violations)
        )
    return violations