"""``Forge.merge_readiness`` — is GitHub/GitLab finished deciding?

WHY this verb exists: after a push, ``gh pr view --json mergeable`` reports
``UNKNOWN`` while GitHub *computes* mergeability, and ``gh pr merge`` refuses
instead of waiting. On 2026-11-08 that refusal burned four merge attempts on
PR #1618 (c1bfcfbf) and three on PR #1621 (e0163dbc) - both were CLEAN /
MERGEABLE with all four required checks SUCCESS and both merged first try by
hand. A state GitHub has not finished computing must therefore read as
``pending`` (retry next tick), never as ``blocked`` (spend an attempt).

The verb fails CLOSED on the READ: an unreadable answer (missing keys, empty
or malformed JSON, non-zero exit, a raised ``OSError``/``TimeoutExpired``) is
``pending``, never ``ready``. ``blocked`` is reserved for a DEFINITIVE
refusal - a conflict, a draft, or branch protection.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from pipeline.forge_github import GitHubForge
from pipeline.forge_gitlab import GitLabForge

BRANCH = "agent/s10-2"
CWD = "/tmp/worktree"


class _Proc:
    """The ``subprocess.run`` result shape the adapters read."""

    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _json(mergeable=None, state=None, *, drop=()):
    """GitHub's ``gh pr view --json mergeable,mergeStateStatus`` payload."""
    payload = {}
    if "mergeable" not in drop:
        payload["mergeable"] = mergeable
    if "mergeStateStatus" not in drop:
        payload["mergeStateStatus"] = state
    return json.dumps(payload)


def _stub_run(monkeypatch, *, stdout=None, returncode=0, raises=None, seen=None):
    """Patch the ONE seam ``merge_pr`` uses (``GitHubForge._run``)."""

    def _run(self, argv, cwd=None, **kw):
        if seen is not None:
            seen.append(list(argv))
        if raises is not None:
            raise raises
        return _Proc(stdout or "", returncode)

    monkeypatch.setattr(GitHubForge, "_run", _run)


# ---------------------------------------------------------------------------
# ready / pending / blocked classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mergeable", "state", "expected", "why"),
    [
        ("MERGEABLE", "CLEAN", "ready", "computed and landable"),
        # The named incident: mergeability not computed yet, clean otherwise.
        ("UNKNOWN", "CLEAN", "pending", "GitHub is still computing mergeability"),
        ("UNKNOWN", "UNKNOWN", "pending", "nothing computed yet"),
        ("MERGEABLE", "UNKNOWN", "pending", "merge state not computed yet"),
        ("MERGEABLE", "BEHIND", "pending", "base moving; not refused"),
        ("MERGEABLE", "DRAFT", "blocked", "a draft PR is a definitive refusal"),
        ("MERGEABLE", "BLOCKED", "blocked", "branch protection refuses"),
        ("CONFLICTING", "DIRTY", "blocked", "a real conflict"),
        ("MERGEABLE", "DIRTY", "blocked", "dirty is definitive"),
    ],
)
def test_github_merge_readiness_classification(
    monkeypatch, mergeable, state, expected, why
):
    _stub_run(monkeypatch, stdout=_json(mergeable, state))
    assert GitHubForge().merge_readiness(CWD, BRANCH) == expected, why


@pytest.mark.parametrize(
    ("mergeable", "state", "expected"),
    [
        ("MERGEABLE", "CLEAN", "ready"),
        ("UNKNOWN", "UNKNOWN", "pending"),
        ("CONFLICTING", "DIRTY", "blocked"),
    ],
)
def test_github_merge_readiness_reads_the_right_json(
    monkeypatch, mergeable, state, expected
):
    seen: list[list[str]] = []
    _stub_run(monkeypatch, stdout=_json(mergeable, state), seen=seen)
    GitHubForge().merge_readiness(CWD, BRANCH)

    assert seen, "merge_readiness must shell out exactly once"
    argv = seen[0]
    assert argv[:3] == ["gh", "pr", "view"], argv
    assert BRANCH == argv[3], argv
    assert "--json" in argv
    fields = argv[argv.index("--json") + 1]
    assert fields == "mergeable,mergeStateStatus", (
        f"merge_readiness must read both fields in ONE call, got {fields!r}"
    )


def test_github_merge_readiness_passes_the_worktree_as_cwd(monkeypatch):
    seen: list[tuple] = []

    def _run(self, argv, cwd=None, **kw):
        seen.append((list(argv), cwd, dict(kw)))
        return _Proc(_json("MERGEABLE", "CLEAN"))

    monkeypatch.setattr(GitHubForge, "_run", _run)
    GitHubForge().merge_readiness(CWD, BRANCH)

    _argv, cwd, kw = seen[0]
    assert cwd == CWD, "the read must run against the story's worktree"
    assert kw.get("check") is not True, (
        "a non-zero exit is an unreadable answer, not an exception: the verb "
        "must classify it as pending itself"
    )


# ---------------------------------------------------------------------------
# Unreadable reads fail CLOSED (pending), never open (ready)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("stdout", "returncode", "label"),
    [
        (json.dumps({}), 0, "missing keys"),
        (json.dumps({"mergeable": "MERGEABLE"}), 0, "missing mergeStateStatus"),
        (json.dumps({"mergeStateStatus": "CLEAN"}), 0, "missing mergeable"),
        ("", 0, "empty stdout"),
        ("not json at all", 0, "malformed JSON"),
        ("[]", 0, "wrong JSON shape"),
        (json.dumps({"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}), 1,
         "non-zero exit"),
        ("", 1, "non-zero exit with no output"),
        (json.dumps({"mergeable": "WAT", "mergeStateStatus": "ALSO_WAT"}), 0,
         "unrecognised vocabulary"),
    ],
)
def test_github_merge_readiness_unreadable_is_pending(
    monkeypatch, stdout, returncode, label
):
    _stub_run(monkeypatch, stdout=stdout, returncode=returncode)
    assert GitHubForge().merge_readiness(CWD, BRANCH) == "pending", (
        f"{label}: an unreadable answer must be pending, never ready"
    )


@pytest.mark.parametrize(
    "exc",
    [
        OSError("gh: not found"),
        subprocess.TimeoutExpired(cmd=["gh", "pr", "view"], timeout=30),
    ],
    ids=["oserror", "timeout"],
)
def test_github_merge_readiness_raised_errors_are_pending(monkeypatch, exc):
    _stub_run(monkeypatch, raises=exc)
    assert GitHubForge().merge_readiness(CWD, BRANCH) == "pending", (
        "the readiness read must never raise and must never fail open"
    )


# ---------------------------------------------------------------------------
# GitLab: same verb, same three answers, same fail-closed READ
# ---------------------------------------------------------------------------
def _stub_glab(monkeypatch, *, stdout=None, returncode=0, raises=None, seen=None):
    """Patch the ONE seam ``merge_pr`` uses on the GitLab adapter."""

    def _run(self, argv, cwd=None, **kw):
        if seen is not None:
            seen.append(list(argv))
        if raises is not None:
            raise raises
        return _Proc(stdout or "", returncode)

    monkeypatch.setattr(GitLabForge, "_run", _run)


def _mr(**fields):
    return json.dumps(fields)


@pytest.mark.parametrize(
    ("payload", "expected", "why"),
    [
        (_mr(detailed_merge_status="mergeable"), "ready", "GitLab says landable"),
        (_mr(merge_status="can_be_merged"), "ready", "legacy clean status"),
        (
            _mr(detailed_merge_status="mergeable", has_conflicts=False),
            "ready",
            "explicitly conflict-free",
        ),
        (
            _mr(detailed_merge_status="checking"),
            "pending",
            "GitLab is still computing mergeability",
        ),
        (_mr(merge_status="checking"), "pending", "legacy still computing"),
        (_mr(detailed_merge_status="unchecked"), "pending", "never computed"),
        (_mr(merge_status="unchecked"), "pending", "legacy never computed"),
        (
            _mr(detailed_merge_status="cannot_be_merged_recheck"),
            "pending",
            "GitLab is re-checking; not a verdict yet",
        ),
        (
            _mr(detailed_merge_status="cannot_be_merged"),
            "blocked",
            "a real conflict is definitive",
        ),
        (_mr(merge_status="cannot_be_merged"), "blocked", "legacy conflict"),
        (_mr(has_conflicts=True), "blocked", "conflict flag is definitive"),
        (_mr(detailed_merge_status="conflict"), "blocked", "conflict status"),
        (_mr(draft=True), "blocked", "a draft MR is a definitive refusal"),
        (_mr(detailed_merge_status="draft_status"), "blocked", "draft status"),
        (_mr(detailed_merge_status="not_approved"), "blocked", "approval gate"),
        (_mr(detailed_merge_status="blocked_status"), "blocked", "blocked status"),
    ],
)
def test_gitlab_merge_readiness_classification(monkeypatch, payload, expected, why):
    _stub_glab(monkeypatch, stdout=payload)
    assert GitLabForge().merge_readiness(CWD, BRANCH) == expected, why


def test_gitlab_merge_readiness_reads_the_mr_json(monkeypatch):
    seen: list[list[str]] = []
    _stub_glab(monkeypatch, stdout=_mr(detailed_merge_status="mergeable"), seen=seen)

    GitLabForge().merge_readiness(CWD, BRANCH)

    argv = seen[0]
    assert argv[:3] == ["glab", "mr", "view"], argv
    assert BRANCH in argv, argv


@pytest.mark.parametrize(
    ("stdout", "returncode", "label"),
    [
        ("", 0, "empty output"),
        ("not json", 0, "malformed JSON"),
        ("[]", 0, "wrong shape"),
        (json.dumps({}), 0, "no mergeability fields at all"),
        (_mr(detailed_merge_status="something_new"), 0, "unrecognised status"),
        (_mr(detailed_merge_status="mergeable"), 1, "non-zero exit"),
    ],
)
def test_gitlab_merge_readiness_unreadable_is_pending(
    monkeypatch, stdout, returncode, label
):
    _stub_glab(monkeypatch, stdout=stdout, returncode=returncode)
    assert GitLabForge().merge_readiness(CWD, BRANCH) == "pending", (
        f"{label}: an unreadable answer must be pending, never ready"
    )


@pytest.mark.parametrize(
    "exc",
    [OSError("glab: not found"), subprocess.TimeoutExpired(cmd=["glab"], timeout=30)],
    ids=["oserror", "timeout"],
)
def test_gitlab_merge_readiness_raised_errors_are_pending(monkeypatch, exc):
    _stub_glab(monkeypatch, raises=exc)
    assert GitLabForge().merge_readiness(CWD, BRANCH) == "pending"
