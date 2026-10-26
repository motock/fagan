"""Tests for ``pipeline/forge_gitlab.GitLabForge``.

The adapter owns every ``glab`` invocation for the forge verbs, all routed
through one runner seam (``_run``) that calls ``subprocess.run``. These tests
patch ``pipeline.forge_gitlab.subprocess.run`` so they observe exactly the
argv the adapter issues, and replay captured JSON fixtures for every verb and
every canonical CI state.

``UNREADABLE`` and ``NONE`` are asserted distinct in both directions: a repo
that declares ``.gitlab-ci.yml`` but whose status cannot be read must never
read as "no CI", because the merge gate treats ``none`` as permission to
merge.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from pipeline import forge as forge_mod
from pipeline.forge import CANCELLED, FAIL, NONE, PASS, PENDING, UNREADABLE
from pipeline.forge_gitlab import GitLabForge

WORKTREE = "/some/worktree"
BRANCH = "feature/x"
TITLE = "X-1: Do the thing"
BODY = "Automated MR for X-1."
SUBJECT = "X-1: Do the thing"
URL = "https://gitlab.com/g/p/-/merge_requests/42"
SHA = "abc123"


class FakeGlab:
    """A fake ``glab``: records argv, replays fixtures, can fail or vanish.

    ``mode="ok"`` replays ``handler(argv)`` as JSON (a ``str`` payload is
    returned verbatim, which is how a job trace arrives). ``mode="error"``
    returns a non-zero exit; ``mode="missing"`` raises ``FileNotFoundError``
    the way a missing binary does.
    """

    def __init__(self, handler=None, *, mode="ok"):
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.mode = mode
        self.handler = handler

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        if self.mode == "missing":
            raise FileNotFoundError("glab")
        if self.mode == "error":
            return subprocess.CompletedProcess(argv, 1, "", "boom")
        payload = self.handler(list(argv)) if self.handler else {}
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.CompletedProcess(argv, 0, stdout, "")


@pytest.fixture
def forge():
    return GitLabForge()


@pytest.fixture
def ci_repo(tmp_path, monkeypatch):
    """A repo root that declares GitLab CI, as the process cwd."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".gitlab-ci.yml").write_text("stages: [test]\n", encoding="utf-8")
    return tmp_path


def _patch(monkeypatch, handler=None, *, mode="ok"):
    fake = FakeGlab(handler, mode=mode)
    monkeypatch.setattr("pipeline.forge_gitlab.subprocess.run", fake)
    return fake


def _is(argv, *prefix):
    return argv[: len(prefix)] == list(prefix)


# ---------------------------------------------------------------------------
# Merge-request verbs
# ---------------------------------------------------------------------------

def test_open_pr_creates_mr(monkeypatch, forge):
    def handler(argv):
        if _is(argv, "glab", "mr", "list"):
            return []
        if _is(argv, "glab", "mr", "create"):
            return {"web_url": URL}
        return {}

    fake = _patch(monkeypatch, handler)

    assert forge.open_pr(WORKTREE, BRANCH, TITLE, BODY) == URL

    # The branch is published first - the protocol's ``open_pr`` contract, and
    # the only way the MR can point at the reviewed SHA - then the lookup runs
    # from the worktree, so no duplicate MR is created.
    assert fake.calls[0] == [
        "git", "push", "--force-with-lease", "-u", "origin", BRANCH,
    ]
    assert fake.kwargs[0]["cwd"] == WORKTREE

    lookup = fake.calls[1]
    assert _is(lookup, "glab", "mr", "list")
    assert "--source-branch" in lookup
    assert lookup[lookup.index("--source-branch") + 1] == BRANCH
    assert fake.kwargs[1]["cwd"] == WORKTREE

    create = [a for a in fake.calls if _is(a, "glab", "mr", "create")]
    assert len(create) == 1
    assert create[0][3:] == [
        "--title", TITLE,
        "--description", BODY,
        "--source-branch", BRANCH,
        "--yes",
    ]


def test_open_pr_returns_existing_mr_when_present(monkeypatch, forge):
    def handler(argv):
        if _is(argv, "glab", "mr", "list"):
            return [{"iid": 42, "web_url": URL}]
        return {}

    fake = _patch(monkeypatch, handler)

    assert forge.open_pr(WORKTREE, BRANCH, TITLE, BODY) == URL
    # Idempotent: a second call re-runs the lookup and still returns the same
    # MR - it must never fall through to a create.
    assert forge.open_pr(WORKTREE, BRANCH, TITLE, BODY) == URL

    assert not [a for a in fake.calls if _is(a, "glab", "mr", "create")]
    assert len([a for a in fake.calls if _is(a, "glab", "mr", "list")]) == 2


def test_update_pr_title(monkeypatch, forge):
    fake = _patch(monkeypatch, lambda argv: {})

    forge.update_pr_title(WORKTREE, BRANCH, TITLE)

    assert fake.calls == [["glab", "mr", "update", BRANCH, "--title", TITLE]]
    assert fake.kwargs[0]["cwd"] == WORKTREE


def test_comment_pr(monkeypatch, forge):
    fake = _patch(monkeypatch, lambda argv: {})

    forge.comment_pr(WORKTREE, BODY)

    assert fake.calls == [["glab", "mr", "note", "--message", BODY]]
    assert fake.kwargs[0]["cwd"] == WORKTREE


def test_merge_pr_passes_subject(monkeypatch, forge):
    fake = _patch(monkeypatch, lambda argv: {})

    forge.merge_pr(WORKTREE, BRANCH, SUBJECT)

    (argv,) = fake.calls
    assert argv[:4] == ["glab", "mr", "merge", BRANCH]
    assert "--squash" in argv
    # The PR title is passed explicitly: without it the squash subject falls
    # back to the branch's own last commit message.
    assert argv[argv.index("--message") + 1] == SUBJECT
    assert fake.kwargs[0]["cwd"] == WORKTREE


def test_pr_title(monkeypatch, forge):
    fake = _patch(monkeypatch, lambda argv: {"title": TITLE})

    assert forge.pr_title(WORKTREE, BRANCH) == TITLE
    assert _is(fake.calls[0], "glab", "mr", "view")
    assert BRANCH in fake.calls[0]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("merged", "MERGED"),
        ("opened", "OPEN"),
        ("closed", "CLOSED"),
        ("locked", "UNKNOWN"),
    ],
)
def test_pr_state_maps_merged_open_closed_and_unknown(monkeypatch, forge, raw, expected):
    _patch(monkeypatch, lambda argv: {"state": raw})

    assert forge.pr_state(URL) == expected


# ---------------------------------------------------------------------------
# CI verbs: every canonical state is reachable
# ---------------------------------------------------------------------------

def test_checks_status_pass(monkeypatch, forge, ci_repo):
    fake = _patch(monkeypatch, lambda argv: [{"id": 100, "status": "success"}])

    assert set(forge.checks_status(BRANCH, SHA).values()) == {PASS}
    assert f"projects/:id/pipelines?sha={SHA}" in fake.calls[0]


def test_checks_status_fail(monkeypatch, forge, ci_repo):
    _patch(monkeypatch, lambda argv: [{"id": 100, "status": "failed"}])

    assert set(forge.checks_status(BRANCH, SHA).values()) == {FAIL}


def test_checks_status_pending(monkeypatch, forge, ci_repo):
    _patch(monkeypatch, lambda argv: [{"id": 100, "status": "running"}])

    assert set(forge.checks_status(BRANCH, SHA).values()) == {PENDING}


def test_checks_status_cancelled(monkeypatch, forge, ci_repo):
    _patch(monkeypatch, lambda argv: [{"id": 100, "status": "canceled"}])

    assert set(forge.checks_status(BRANCH, SHA).values()) == {CANCELLED}


def test_checks_status_manual_maps_to_pass(monkeypatch, forge, ci_repo):
    """A deliberately-gated ``manual`` job is a PASS, not PENDING/UNKNOWN."""
    _patch(monkeypatch, lambda argv: [{"id": 100, "status": "manual"}])

    assert set(forge.checks_status(BRANCH, SHA).values()) == {PASS}


def test_checks_status_unreadable_when_gitlab_ci_exists_and_api_fails(
    monkeypatch, forge, ci_repo
):
    _patch(monkeypatch, mode="error")

    result = forge.checks_status(BRANCH, SHA)

    assert set(result.values()) == {UNREADABLE}
    # "could not read" must never collapse into "no CI".
    assert UNREADABLE != NONE


def test_checks_status_none_when_no_gitlab_ci(monkeypatch, forge, tmp_path):
    monkeypatch.chdir(tmp_path)
    fake = _patch(monkeypatch, mode="error")

    result = forge.checks_status(BRANCH, SHA)

    assert set(result.values()) == {NONE}
    assert NONE != UNREADABLE
    # No CI config: the API is never called, so a broken glab cannot matter.
    assert fake.calls == []


def test_failure_excerpt_reads_failed_job_trace(monkeypatch, forge, ci_repo):
    def handler(argv):
        path = argv[-1]
        if path.startswith("projects/:id/pipelines?"):
            return [{"id": 100, "status": "failed"}]
        if path == "projects/:id/pipelines/100/jobs":
            return [
                {"id": 201, "status": "success"},
                {"id": 202, "status": "failed"},
            ]
        if path == "projects/:id/jobs/202/trace":
            return "error: boom\n"
        return {}

    fake = _patch(monkeypatch, handler)

    excerpt = forge.failure_excerpt(BRANCH)

    assert excerpt is not None
    assert "boom" in excerpt
    # The *failed* job's trace, not the first job's and not the pipeline's.
    assert any(a[-1] == "projects/:id/jobs/202/trace" for a in fake.calls)
    assert not any("jobs/201/trace" in a[-1] for a in fake.calls)
    assert not any("pipelines/100/trace" in a[-1] for a in fake.calls)


def test_rerun_failed_posts_retry(monkeypatch, forge, ci_repo):
    def handler(argv):
        if argv[-1].startswith("projects/:id/pipelines?"):
            return [{"id": 100, "status": "failed"}]
        return {}

    fake = _patch(monkeypatch, handler)

    assert forge.rerun_failed(SHA) is True

    retry = [a for a in fake.calls if a[-1].endswith("/retry")]
    assert len(retry) == 1
    assert "POST" in retry[0]
    assert retry[0][-1] == "projects/:id/pipelines/100/retry"
    # A pipeline retry, never a single job retry.
    assert not any("jobs" in a[-1] for a in retry)


def test_missing_glab_binary_yields_unreadable_only_when_gitlab_ci_exists(
    monkeypatch, forge, tmp_path
):
    monkeypatch.chdir(tmp_path)
    fake = _patch(monkeypatch, mode="missing")

    # No .gitlab-ci.yml: NONE, and glab is never invoked.
    assert set(forge.checks_status(BRANCH, SHA).values()) == {NONE}
    assert fake.calls == []

    # The same missing binary, now with CI declared, is UNREADABLE - never NONE.
    (tmp_path / ".gitlab-ci.yml").write_text("stages: [test]\n", encoding="utf-8")
    assert set(forge.checks_status(BRANCH, SHA).values()) == {UNREADABLE}
    assert fake.calls, "the pipelines API must be attempted when CI is declared"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_get_forge_returns_gitlab_forge_for_gitlab_remote(monkeypatch):
    from pipeline.forge_github import GitHubForge

    forge_mod._CACHE.clear()
    monkeypatch.delenv("PIPELINE_FORGE", raising=False)
    try:
        monkeypatch.setattr(
            forge_mod, "_remote_url", lambda root: "git@gitlab.com:group/proj.git"
        )
        assert isinstance(forge_mod.get_forge("/repo"), GitLabForge)

        # Registering gitlab must not displace github.
        forge_mod._CACHE.clear()
        monkeypatch.setattr(
            forge_mod, "_remote_url", lambda root: "git@github.com:org/repo.git"
        )
        assert isinstance(forge_mod.get_forge("/repo"), GitHubForge)
    finally:
        forge_mod._CACHE.clear()
