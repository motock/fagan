"""``GitLabForge.open_pr`` must publish the branch, like ``GitHubForge`` does.

``pipeline/forge.py`` declares ``open_pr`` as "Push ``branch`` and open a PR;
return an opaque handle", and nothing upstream of the verb pushes the branch.
Without the push a GitLab merge request is either impossible to create (the
source branch does not exist on origin yet) or points at the pre-review SHA,
because every resumed dispatch rebases the branch and rewrites its commits.

These tests live in their own module rather than in ``test_forge_gitlab.py``
so the pre-existing adapter tests stay untouched; they patch the same runner
seam (``pipeline.forge_gitlab.subprocess.run``) and therefore observe exactly
the argv the adapter issues.
"""

from __future__ import annotations

import subprocess

import pytest

from pipeline.forge_gitlab import GitLabForge

WORKTREE = "/some/worktree"
BRANCH = "feature/x"
TITLE = "X-1: Do the thing"
BODY = "Automated MR for X-1."
URL = "https://gitlab.com/g/p/-/merge_requests/42"

PUSH_ARGV = ["git", "push", "--force-with-lease", "-u", "origin", BRANCH]


class FakeRunner:
    """A fake ``subprocess.run`` that honours ``check=`` like the real one.

    ``fail`` names the argv prefix that exits non-zero; every other call
    succeeds. ``check=True`` on a failing call raises
    ``subprocess.CalledProcessError``, which is what the adapter relies on to
    surface a failed push or a failed ``glab mr create``.
    """

    def __init__(self, handler=None, *, fail=None):
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.handler = handler
        self.fail = fail

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        self.kwargs.append(kwargs)
        returncode = 1 if self.fail and argv[: len(self.fail)] == list(self.fail) else 0
        stdout = ""
        if returncode == 0 and self.handler:
            payload = self.handler(argv)
            stdout = payload if isinstance(payload, str) else __import__("json").dumps(payload)
        proc = subprocess.CompletedProcess(argv, returncode, stdout, "boom")
        if returncode != 0 and kwargs.get("check"):
            raise subprocess.CalledProcessError(returncode, argv, stdout, "boom")
        return proc


@pytest.fixture
def forge():
    return GitLabForge()


def _patch(monkeypatch, handler=None, *, fail=None):
    fake = FakeRunner(handler, fail=fail)
    monkeypatch.setattr("pipeline.forge_gitlab.subprocess.run", fake)
    return fake


def _is(argv, *prefix):
    return argv[: len(prefix)] == list(prefix)


def _mr_handler(argv):
    if _is(argv, "glab", "mr", "list"):
        return []
    if _is(argv, "glab", "mr", "create"):
        return {"web_url": URL}
    return {}


# ---------------------------------------------------------------------------
# The push
# ---------------------------------------------------------------------------

def test_open_pr_pushes_branch_before_lookup(monkeypatch, forge):
    fake = _patch(monkeypatch, _mr_handler)

    assert forge.open_pr(WORKTREE, BRANCH, TITLE, BODY) == URL

    # The protocol promises the push, so it must be the first thing issued.
    assert fake.calls[0] == PUSH_ARGV
    assert fake.kwargs[0]["cwd"] == WORKTREE
    assert fake.kwargs[0]["check"] is True

    # ... and it must precede the lookup: an existing MR is returned as-is,
    # so a push after the lookup would leave a rebased branch unpublished.
    assert _is(fake.calls[1], "glab", "mr", "list")


def test_open_pr_pushes_even_when_mr_already_exists(monkeypatch, forge):
    def handler(argv):
        if _is(argv, "glab", "mr", "list"):
            return [{"iid": 42, "web_url": URL}]
        return {}

    fake = _patch(monkeypatch, handler)

    assert forge.open_pr(WORKTREE, BRANCH, TITLE, BODY) == URL

    assert fake.calls[0] == PUSH_ARGV
    assert not [a for a in fake.calls if _is(a, "glab", "mr", "create")]


def test_open_pr_raises_when_push_fails(monkeypatch, forge):
    fake = _patch(monkeypatch, _mr_handler, fail=["git", "push"])

    with pytest.raises(subprocess.CalledProcessError):
        forge.open_pr(WORKTREE, BRANCH, TITLE, BODY)

    # A failed push must abort before an MR is created for an unpublished
    # branch.
    assert not [a for a in fake.calls if _is(a, "glab", "mr", "create")]


# ---------------------------------------------------------------------------
# The failure path
# ---------------------------------------------------------------------------

def test_open_pr_raises_when_mr_create_fails(monkeypatch, forge):
    _patch(monkeypatch, _mr_handler, fail=["glab", "mr", "create"])

    # Returning "" here would be stored as the MR URL and the tick would
    # carry on as if the MR existed.
    with pytest.raises(subprocess.CalledProcessError):
        forge.open_pr(WORKTREE, BRANCH, TITLE, BODY)


# ---------------------------------------------------------------------------
# has_ci_config keeps consulting the process repo
# ---------------------------------------------------------------------------
# ``has_ci_config`` deliberately unions the passed root, the repo ``glab``
# queries (``_repo_cwd``) and the process cwd. The union is the fail-safe
# direction: "CI is configured" yields UNREADABLE/PENDING and never NONE,
# which would permit merging without checks. The pre-existing CI tests in
# ``test_forge_gitlab.py`` pin that behaviour by chdir-ing into a CI repo, so
# it is left as it is.

def test_has_ci_config_true_when_scoped_repo_declares_ci(monkeypatch, tmp_path):
    scoped = tmp_path / "scoped"
    scoped.mkdir()
    (scoped / ".gitlab-ci.yml").write_text("stages: [test]\n", encoding="utf-8")
    monkeypatch.setattr("pipeline.forge_gitlab._repo_cwd", lambda: str(scoped))

    assert GitLabForge().has_ci_config(str(tmp_path / "elsewhere")) is True
