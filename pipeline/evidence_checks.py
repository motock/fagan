"""Evidence checks: machine-readable receipts for authority decisions.

Each check returns a single *Evidence* dict::

    {"check": str, "passed": bool, "ref": str, "detail": str, "sha256": str}

``detail`` names identifiers and counts only -- paths, counts, shas, error
types.  It never carries file contents, diffs, prompts or log text, because
evidence is archived and quoted out of context.  ``sha256`` is the hex digest
of the canonical JSON (``sort_keys=True``, ``separators=(",", ":")``) of
``{"check", "passed", "ref", "detail"}``; extra keys such as ``base_sha`` or
``verdict_trust`` are deliberately outside the digest.

A check never raises.  An exception, timeout or unreadable input yields
``passed False`` and a ``detail`` naming the error *type* only, never the
message (messages can embed URLs and file content).

Scope of the judgement
----------------------
Checks 5-7 (:func:`check_no_test_deleted`, :func:`check_assertions_not_reduced`
and :func:`check_nothing_skipped`) are the ONLY judgement this library makes
about edits to existing tests.  Nothing here decides whether a test edit is
"reasonable".

The companion rule -- *expected values may not change unless the story names
them* -- needs a named-values input that does not exist yet; it is a later
story and is NOT implemented here.  Do not add a heuristic that guesses it.

Unlike :func:`pipeline.scope_gate.check_branch_scope`, which fails OPEN on a
git error (right for a reviewer, wrong for an authority check), every check
here fails CLOSED: an unverifiable fact is a failed evidence item.
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from collections.abc import Mapping
from typing import Any

from pipeline.scope_gate import is_test_path, scope_violations

#: ``detail`` is bounded so an evidence record stays a one-line receipt.
DETAIL_MAX = 200
#: Wall-clock ceiling for every git call this module makes.
GIT_TIMEOUT_SECONDS = 60
#: How many offending paths a ``detail`` names before it degrades to a count.
MAX_NAMED_PATHS = 5

PASS_STATE = "pass"
PYTHON_TEST_SUFFIX = ".py"


# ---------------------------------------------------------------------------
# envelope
# ---------------------------------------------------------------------------


def _evidence(
    check: str,
    passed: bool,
    ref: str,
    detail: str,
    **extra: Any,
) -> dict[str, Any]:
    """Build one Evidence dict.

    ``detail`` is truncated *before* hashing, so an independent recomputation
    over the returned ``detail`` always agrees.  Only the four canonical keys
    are hashed; ``extra`` is presentation.
    """
    passed = bool(passed)
    ref = ref if isinstance(ref, str) else str(ref)
    detail = str(detail)[:DETAIL_MAX]
    canonical = json.dumps(
        {"check": check, "passed": passed, "ref": ref, "detail": detail},
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence: dict[str, Any] = {
        "check": check,
        "passed": passed,
        "ref": ref,
        "detail": detail,
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
    evidence.update(extra)
    return evidence


def _failure(
    check: str, ref: str, exc: BaseException, **extra: Any
) -> dict[str, Any]:
    """Fail closed, naming the error type only (never the message)."""
    return _evidence(check, False, ref, type(exc).__name__, **extra)


# ---------------------------------------------------------------------------
# 1. merge base
# ---------------------------------------------------------------------------


def check_merge_base(
    worktree: str,
    head_sha: str,
    base_branch: str,
    fetch: bool = True,
) -> dict[str, Any]:
    """Fetch ``origin/<base_branch>`` and report ``merge-base`` with the head.

    A failed fetch, a failed merge-base or a timeout fails closed.  There is
    deliberately NO fallback to a possibly stale local ``origin/<base_branch>``
    ref: that silent fallback is the drift defect this check exists to stop.
    The successful fetch advances ``refs/remotes/origin/<base_branch>``, so a
    later ``fetch=False`` call sees the corrected ref.
    """
    check = "check_merge_base"
    empty = {"base_sha": ""}

    if fetch:
        try:
            fetched = subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "fetch",
                    "--no-tags",
                    "origin",
                    base_branch,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            return _failure(check, head_sha, exc, **empty)
        except OSError as exc:
            return _failure(check, head_sha, exc, **empty)
        if fetched.returncode != 0:
            return _evidence(check, False, head_sha, "fetch_failed", **empty)

    try:
        merged = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "merge-base",
                head_sha,
                f"origin/{base_branch}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return _failure(check, head_sha, exc, **empty)
    except OSError as exc:
        return _failure(check, head_sha, exc, **empty)

    if merged.returncode != 0:
        return _evidence(check, False, head_sha, "merge_base_failed", **empty)
    base_sha = merged.stdout.strip()
    if not base_sha:
        return _evidence(check, False, head_sha, "merge_base_failed", **empty)
    return _evidence(
        check, True, head_sha, f"base_sha={base_sha}", base_sha=base_sha
    )


# ---------------------------------------------------------------------------
# 2. scope
# ---------------------------------------------------------------------------


def _base_top_level_set(base_top_level: Any) -> set[str]:
    if isinstance(base_top_level, str):
        return {base_top_level} if base_top_level else set()
    try:
        return set(base_top_level)
    except TypeError:
        return set()


def _base_top_level_ref(base_top_level: Any) -> str:
    if isinstance(base_top_level, str):
        return base_top_level or "base_top_level"
    try:
        joined = ",".join(sorted(str(item) for item in base_top_level))
    except TypeError:
        return type(base_top_level).__name__
    return joined or "base_top_level"


def _violation_path(line: str) -> str:
    """Take the path out of a ``scope_violations`` prose line."""
    return line.split(":", 1)[0]


def check_scope(
    changed_paths: Any,
    declared_files: Any,
    base_top_level: Any,
    test_globs: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Grade ``changed_paths`` against the story's declared ``files``.

    Delegates the classification to the pure
    :func:`pipeline.scope_gate.scope_violations`; ``detail`` names the count
    and at most the first five violating paths.
    """
    check = "check_scope"
    ref = _base_top_level_ref(base_top_level)
    try:
        violations = scope_violations(
            list(changed_paths),
            list(declared_files),
            _base_top_level_set(base_top_level),
            tuple(test_globs),
        )
        detail = f"violations={len(violations)}"
        if violations:
            paths = [_violation_path(line) for line in violations]
            detail += ":" + ",".join(paths[:MAX_NAMED_PATHS])
        return _evidence(check, not violations, ref, detail)
    except Exception as exc:  # noqa: BLE001 - a check must never raise
        return _failure(check, ref, exc)


# ---------------------------------------------------------------------------
# 3. CI
# ---------------------------------------------------------------------------


def _ci_states(snapshot: Any) -> dict[str, Any]:
    """Normalise a CISnapshot, a Mapping or an object exposing ``.checks``."""
    if snapshot is None:
        return {}
    if isinstance(snapshot, Mapping):
        return dict(snapshot)
    checks = getattr(snapshot, "checks", None)
    if isinstance(checks, Mapping):
        return dict(checks)
    return dict(snapshot)


def check_ci_green(snapshot: Any, head_sha: str) -> dict[str, Any]:
    """Pass only when the snapshot is non-empty and every state is ``pass``.

    ``all()`` over an empty mapping is ``True``, so the explicit non-empty
    guard keeps "no CI" from reading as "green".  ``none``, ``unreadable``,
    ``pending``, ``cancelled`` and ``fail`` all fail.
    """
    check = "check_ci_green"
    try:
        states = _ci_states(snapshot)
        counts: dict[str, int] = {}
        for state in states.values():
            label = str(state)
            counts[label] = counts.get(label, 0) + 1
        if counts:
            detail = ",".join(f"{label}={counts[label]}" for label in sorted(counts))
        else:
            detail = "total=0"
        passed = bool(states) and all(
            str(state) == PASS_STATE for state in states.values()
        )
        return _evidence(check, passed, head_sha, detail)
    except Exception as exc:  # noqa: BLE001 - a check must never raise
        return _failure(check, head_sha, exc)


# ---------------------------------------------------------------------------
# 4. reviewer verdict
# ---------------------------------------------------------------------------


def _story_field(story: Any, key: str) -> Any:
    if story is None:
        return None
    try:
        return story.get(key)
    except AttributeError:
        return getattr(story, key, None)


def check_reviewer_approve(story: Any, head_sha: str) -> dict[str, Any]:
    """Pass iff the story's verdict approves *this* sha.

    Missing keys fail closed: a missing ``last_reviewed_sha`` is never
    defaulted to the head sha (that would fail open).
    """
    check = "check_reviewer_approve"
    # ``verdict_trust`` marks the verdict as an unverified claim.  The
    # review-ground-truth plan (stale async verdict) must land and flip this
    # to "verified" before an overlord may rely on it; the S9 story checks
    # this key.
    extra = {"verdict_trust": "unverified"}
    try:
        verdict = _story_field(story, "review_verdict")
        reviewed = _story_field(story, "last_reviewed_sha")
        passed = verdict == "APPROVE" and reviewed == head_sha
        same = "yes" if reviewed == head_sha else "no"
        detail = f"verdict={verdict};reviewed_sha_match={same}"
        return _evidence(check, passed, head_sha, detail, **extra)
    except Exception as exc:  # noqa: BLE001 - a check must never raise
        return _failure(check, head_sha, exc, **extra)


# ---------------------------------------------------------------------------
# git plumbing shared by checks 5-7
# ---------------------------------------------------------------------------


class _GitError(RuntimeError):
    """A git command needed for evidence exited non-zero."""


def _git(worktree: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )


def _name_status(
    worktree: str, base_sha: str, head_sha: str
) -> list[tuple[str, str]]:
    """Return ``(status_letter, path)`` pairs for ``base..head``.

    ``--no-renames`` makes a move show up as ``D`` plus ``A``, which is the
    conservative reading downstream.  Fields are split on tabs so a path
    with spaces survives intact.
    """
    proc = _git(
        worktree,
        "diff",
        "--name-status",
        "--no-renames",
        f"{base_sha}..{head_sha}",
    )
    if proc.returncode != 0:
        raise _GitError("git diff --name-status failed")
    entries: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        entries.append((parts[0][0], parts[-1]))
    return entries


def _show(worktree: str, sha: str, path: str) -> str | None:
    """Return ``sha:path`` content, or ``None`` when it cannot be read."""
    proc = _git(worktree, "show", f"{sha}:{path}")
    if proc.returncode != 0:
        return None
    return proc.stdout


def _changed_test_files_present_at_base(
    worktree: str, base_sha: str, entries: list[tuple[str, str]]
) -> list[str]:
    """Changed test paths whose base version exists, de-duplicated, sorted.

    A pure deletion is included on purpose: it is present at base and missing
    at head, so checks 6 and 7 fail closed on it.
    """
    candidates: list[str] = []
    seen: set[str] = set()
    for _status, path in entries:
        if path in seen or not is_test_path(path):
            continue
        seen.add(path)
        if _show(worktree, base_sha, path) is None:
            continue
        candidates.append(path)
    return sorted(candidates)


# ---------------------------------------------------------------------------
# 5. no test deleted
# ---------------------------------------------------------------------------


def check_no_test_deleted(
    worktree: str, base_sha: str, head_sha: str
) -> dict[str, Any]:
    """Pass iff no deleted path is a test path.

    A rename is reported as delete plus add by ``--no-renames``, so a moved
    test counts as deleted: conservative and documented.
    """
    check = "check_no_test_deleted"
    ref = f"{base_sha}..{head_sha}"
    try:
        entries = _name_status(worktree, base_sha, head_sha)
    except _GitError:
        return _evidence(check, False, ref, "diff_failed")
    except Exception as exc:  # noqa: BLE001 - a check must never raise
        return _failure(check, ref, exc)

    deleted = sorted(
        path
        for status, path in entries
        if status == "D" and is_test_path(path)
    )
    detail = f"deleted_tests={len(deleted)}"
    if deleted:
        detail += ":" + ",".join(deleted[:MAX_NAMED_PATHS])
    return _evidence(check, not deleted, ref, detail)


# ---------------------------------------------------------------------------
# 6. assertions not reduced
# ---------------------------------------------------------------------------


def _count_assertions(source: str) -> int | None:
    """Count assertion constructs, or ``None`` when *source* does not parse.

    ``ast.Assert`` statements, calls to an attribute starting with
    ``assert`` (``self.assertEqual(...)``) and ``pytest.raises`` /
    ``pytest.warns`` contexts all count.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            count += 1
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            is_pytest_context = (
                func.attr in {"raises", "warns"}
                and isinstance(func.value, ast.Name)
                and func.value.id == "pytest"
            )
            if func.attr.startswith("assert") or is_pytest_context:
                count += 1
    return count


def check_assertions_not_reduced(
    worktree: str, base_sha: str, head_sha: str
) -> dict[str, Any]:
    """Pass iff every changed Python test file keeps its assertion count.

    Per file, never per changeset: an assertion moved from one test file to
    another is a reduction of the file it left.  A non-Python test file, an
    unparseable side or a file missing at head fails closed with
    ``unsupported_or_unparseable:<count>``.
    """
    check = "check_assertions_not_reduced"
    ref = f"{base_sha}..{head_sha}"
    try:
        entries = _name_status(worktree, base_sha, head_sha)
        paths = _changed_test_files_present_at_base(worktree, base_sha, entries)
    except _GitError:
        return _evidence(check, False, ref, "diff_failed")
    except Exception as exc:  # noqa: BLE001 - a check must never raise
        return _failure(check, ref, exc)

    offenders: list[str] = []
    reduced: list[str] = []
    for path in paths:
        if not path.endswith(PYTHON_TEST_SUFFIX):
            offenders.append(path)
            continue
        base_source = _show(worktree, base_sha, path)
        head_source = _show(worktree, head_sha, path)
        if base_source is None or head_source is None:
            offenders.append(path)
            continue
        base_count = _count_assertions(base_source)
        head_count = _count_assertions(head_source)
        if base_count is None or head_count is None:
            offenders.append(path)
            continue
        if head_count < base_count:
            reduced.append(f"{path}({base_count}->{head_count})")

    if offenders:
        return _evidence(
            check, False, ref, f"unsupported_or_unparseable:{len(offenders)}"
        )
    if reduced:
        detail = f"reduced={len(reduced)}:" + ",".join(reduced[:MAX_NAMED_PATHS])
        return _evidence(check, False, ref, detail)
    return _evidence(check, True, ref, f"checked={len(paths)}")


# ---------------------------------------------------------------------------
# 7. nothing skipped
# ---------------------------------------------------------------------------


def _is_skip_mark(node: ast.AST) -> bool:
    """True for ``pytest.mark.skip*`` / ``pytest.mark.xfail`` attribute nodes."""
    if not isinstance(node, ast.Attribute):
        return False
    if not isinstance(node.value, ast.Attribute):
        return False
    if node.value.attr != "mark":
        return False
    if not isinstance(node.value.value, ast.Name):
        return False
    if node.value.value.id != "pytest":
        return False
    return node.attr == "xfail" or node.attr.startswith("skip")


def _is_pytest_skip_call(node: ast.AST) -> bool:
    """True for ``pytest.skip(...)`` / ``pytest.xfail(...)`` calls."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
        and node.func.attr in {"skip", "xfail"}
    )


def _count_skips(source: str) -> int | None:
    """Count skip/xfail uses by AST, or ``None`` when *source* does not parse.

    Counts ``pytest.mark.skip*``, ``pytest.mark.xfail``, and ``pytest.skip(``
    / ``pytest.xfail(`` calls.  Counting AST nodes (not the literal text
    ``skip``) keeps comments and string literals from inflating the tally.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    return sum(
        1
        for node in ast.walk(tree)
        if _is_skip_mark(node) or _is_pytest_skip_call(node)
    )


def check_nothing_skipped(
    worktree: str, base_sha: str, head_sha: str
) -> dict[str, Any]:
    """Pass iff no changed Python test file gains skip/xfail uses.

    Uses the same file set as :func:`check_assertions_not_reduced`.  A
    non-Python test file, an unparseable side or a file missing at head fails
    closed with ``unsupported_or_unparseable:<count>``.
    """
    check = "check_nothing_skipped"
    ref = f"{base_sha}..{head_sha}"
    try:
        entries = _name_status(worktree, base_sha, head_sha)
        paths = _changed_test_files_present_at_base(worktree, base_sha, entries)
    except _GitError:
        return _evidence(check, False, ref, "diff_failed")
    except Exception as exc:  # noqa: BLE001 - a check must never raise
        return _failure(check, ref, exc)

    offenders: list[str] = []
    increased: list[str] = []
    for path in paths:
        if not path.endswith(PYTHON_TEST_SUFFIX):
            offenders.append(path)
            continue
        base_source = _show(worktree, base_sha, path)
        head_source = _show(worktree, head_sha, path)
        if base_source is None or head_source is None:
            offenders.append(path)
            continue
        base_count = _count_skips(base_source)
        head_count = _count_skips(head_source)
        if base_count is None or head_count is None:
            offenders.append(path)
            continue
        if head_count > base_count:
            increased.append(f"{path}({base_count}->{head_count})")

    if offenders:
        return _evidence(
            check, False, ref, f"unsupported_or_unparseable:{len(offenders)}"
        )
    if increased:
        detail = f"increased={len(increased)}:" + ",".join(
            increased[:MAX_NAMED_PATHS]
        )
        return _evidence(check, False, ref, detail)
    return _evidence(check, True, ref, f"checked={len(paths)}")


__all__ = [
    "DETAIL_MAX",
    "check_assertions_not_reduced",
    "check_ci_green",
    "check_merge_base",
    "check_no_test_deleted",
    "check_nothing_skipped",
    "check_reviewer_approve",
    "check_scope",
]
