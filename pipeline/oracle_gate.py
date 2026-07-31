"""Pre-dispatch acceptance-oracle validation.

A broken acceptance fixture (one that cannot pass no matter what an
implementer writes - a syntax error in a helper, an XML parser rejecting a
comment, a bad CLI invocation) is the most expensive single failure mode in
the pipeline: it consumes an implementer's entire step budget and produces no
usable signal, because the grader itself never reaches a real assertion.

classify_oracle_outcome distinguishes that case (state "errors") from a
USEFUL failure - the fixture ran fine and correctly reports that the
deliverable is missing (state "fails_correctly", e.g. a plain assertion
failure or a ModuleNotFoundError for a not-yet-written module, which pytest
reports as a collection error but which is the CORRECT pre-dispatch state,
not a broken oracle) - from a fixture that is already satisfied with no
implementation at all (state "passes") or that collected no tests (state
"empty").

validate_acceptance_fixtures runs a story's declared acceptance fixtures
against a given checkout (scoped via build_detect's existing runner-detection
helpers, the same ones used for grading) and classifies the result - so a
broken oracle can be caught before an implementer is even launched.

acceptance_digests records a sha256 of each fixture's AUTHORITATIVE manifest
source at dispatch time, so a later gate (see pipeline.ci._acceptance_tampered)
can prove the worktree copy was not rewritten - the read-only oracle is
described as read-only in prompt text only; this is the mechanism behind it.
"""

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from .build_detect import _scope_test_cmd_to_acceptance, detect_test_command

_ORACLE_ERROR_MARKERS = (
    "INTERNALERROR",
    "xml.parsers.expat",
    "ExpatError",
    "usage: pytest",
    "unrecognized arguments",
)


def classify_oracle_outcome(returncode: int, output: str) -> dict:
    """Classify a test-runner outcome as "passes", "empty", "errors", or
    "fails_correctly". Always returns a dict with string "state" and "detail".
    """
    if returncode == 0:
        return {"state": "passes", "detail": "acceptance fixture already passes"}
    if returncode == 5:
        return {"state": "empty", "detail": "no tests were collected"}
    for marker in _ORACLE_ERROR_MARKERS:
        if marker in output:
            return {
                "state": "errors",
                "detail": f"oracle is broken ({marker}): {output[-500:]}",
            }
    return {
        "state": "fails_correctly",
        "detail": f"fixture fails as expected: {output[-500:]}",
    }


def validate_acceptance_fixtures(story: dict[str, Any], checkout: Path) -> dict:
    """Run story's acceptance fixtures against checkout and classify the
    result. Returns a dict with "state", "detail", and "paths" (the
    worktree-relative fixture paths that were graded). Never raises - a
    runner that cannot even be invoked is reported as state "errors".
    """
    acceptance = story.get("acceptance") or []
    paths = [entry["path"] for entry in acceptance]
    if not acceptance:
        return {"state": "none", "detail": "story has no acceptance fixtures", "paths": []}

    try:
        test_dir, test_cmd = detect_test_command(Path(checkout))
        abs_paths = [str(Path(checkout) / p) for p in paths]
        scoped = _scope_test_cmd_to_acceptance(test_cmd, abs_paths, test_dir)
        cmd = scoped if scoped is not None else test_cmd
        result = subprocess.run(
            cmd, cwd=test_dir, capture_output=True, text=True, check=False
        )
        classified = classify_oracle_outcome(
            result.returncode, result.stdout + result.stderr
        )
    except Exception as exc:  # noqa: BLE001 (must never raise; report as broken)
        return {"state": "errors", "detail": f"{type(exc).__name__}: {exc}", "paths": paths}

    return {**classified, "paths": paths}


def acceptance_digests(story: dict[str, Any]) -> dict[str, str]:
    """Map each acceptance entry's path to a sha256 hex digest of its
    AUTHORITATIVE manifest `source` - never of the file on disk, which could
    already have been rewritten. Returns {} when the story has no acceptance
    entries."""
    return {
        entry["path"]: hashlib.sha256(entry["source"].encode()).hexdigest()
        for entry in (story.get("acceptance") or [])
    }


__all__ = [
    "acceptance_digests",
    "classify_oracle_outcome",
    "validate_acceptance_fixtures",
]
