"""Tests for the retirement of PIPELINE_REVIEW_MAX_TOKENS /
PIPELINE_SECURITY_REVIEW_MAX_TOKENS.

These env vars used to be read in pipeline/review.py and passed as
``max_tokens=`` into the backend's ``complete()`` call for both the ordinary
reviewer (``_run_reviewer``) and the security reviewer
(``_run_security_reviewer``). They are now retired: the value is no longer
read, no longer passed, and no longer documented in REFERENCE.md.

The tests mock the backend the same way the existing review tests do (see
test_pipeline_review_prompt_addition.py) and assert that setting the env
vars to an unusual value has NO observable effect on the ``complete()``
kwargs -- i.e. ``max_tokens`` is absent from the call entirely.
"""

from pathlib import Path

from app import pipeline_mcp_server as p

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

class _FakeDriver:
    """Records the kwargs passed to complete() and returns a parseable verdict."""

    def __init__(self):
        self.kwargs = None
        self.prompt = None

    def complete(self, prompt, **kwargs):
        self.prompt = prompt
        self.kwargs = kwargs
        return "VERDICT: APPROVE"


def _install_fake_backend(monkeypatch):
    """Install a fake backend whose get_backend returns a recording driver."""
    fake = _FakeDriver()
    monkeypatch.setattr(
        p.backend, "get_backend", lambda role, name=None: fake
    )
    return fake


def _worktree_with_cell_dir(tmp_path):
    """Build a worktree path whose parent dir is named 'worktrees' so the
    cell_dir branch is exercised (mirrors production layout)."""
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    wt = worktrees / "some-story"
    wt.mkdir()
    return str(wt)


# --------------------------------------------------------------------------- #
# _run_reviewer
# --------------------------------------------------------------------------- #

def test_run_reviewer_does_not_pass_max_tokens(monkeypatch, tmp_path):
    """PIPELINE_REVIEW_MAX_TOKENS set to an unusual value must NOT appear as a
    max_tokens kwarg on the complete() call."""
    monkeypatch.setenv("PIPELINE_REVIEW_MAX_TOKENS", "7777")
    fake = _install_fake_backend(monkeypatch)
    p._run_reviewer(str(tmp_path / "wt"), "agent/some-branch")
    assert fake.kwargs is not None, "complete() was never called"
    assert "max_tokens" not in fake.kwargs, (
        f"max_tokens should be retired but was passed: {fake.kwargs!r}"
    )


def test_run_reviewer_preserves_other_kwargs(monkeypatch, tmp_path):
    """Removing max_tokens must not disturb the other call arguments."""
    monkeypatch.setenv("PIPELINE_REVIEW_MAX_TOKENS", "7777")
    fake = _install_fake_backend(monkeypatch)
    p._run_reviewer(str(tmp_path / "wt"), "agent/some-branch")
    assert fake.kwargs is not None
    # The other documented kwargs must still be present and unchanged.
    for key in ("system", "model", "allowed_tools", "cwd", "cell_dir"):
        assert key in fake.kwargs, f"missing expected kwarg {key!r}: {fake.kwargs!r}"
    assert fake.kwargs["allowed_tools"] == "Bash,Read"
    assert fake.kwargs["cwd"] == str(tmp_path / "wt")


def test_run_reviewer_cell_dir_branch_unchanged(monkeypatch, tmp_path):
    """The cell_dir computation logic must be untouched by the retirement."""
    monkeypatch.setenv("PIPELINE_REVIEW_MAX_TOKENS", "7777")
    fake = _install_fake_backend(monkeypatch)
    wt = _worktree_with_cell_dir(tmp_path)
    p._run_reviewer(wt, "agent/some-branch")
    assert fake.kwargs is not None
    assert "max_tokens" not in fake.kwargs
    assert fake.kwargs["cell_dir"] == str(Path(wt).resolve().parent)


def test_run_reviewer_unusual_value_has_no_effect(monkeypatch, tmp_path):
    """A wildly different cap value must produce identical (max_tokens-free)
    kwargs as the default/unset case -- the var is a pure no-op now."""
    # First call with the var set to something unusual.
    monkeypatch.setenv("PIPELINE_REVIEW_MAX_TOKENS", "999999")
    fake_set = _install_fake_backend(monkeypatch)
    p._run_reviewer(str(tmp_path / "wt"), "agent/some-branch")
    kwargs_when_set = dict(fake_set.kwargs)
    assert "max_tokens" not in kwargs_when_set

    # Second call with the var unset entirely.
    monkeypatch.delenv("PIPELINE_REVIEW_MAX_TOKENS", raising=False)
    fake_unset = _install_fake_backend(monkeypatch)
    p._run_reviewer(str(tmp_path / "wt"), "agent/some-branch")
    kwargs_when_unset = dict(fake_unset.kwargs)
    assert "max_tokens" not in kwargs_when_unset

    # Everything observable must be identical regardless of the env var.
    assert kwargs_when_set == kwargs_when_unset


# --------------------------------------------------------------------------- #
# _run_security_reviewer
# --------------------------------------------------------------------------- #

def test_run_security_reviewer_does_not_pass_max_tokens(monkeypatch, tmp_path):
    """Neither PIPELINE_SECURITY_REVIEW_MAX_TOKENS nor its fallback
    PIPELINE_REVIEW_MAX_TOKENS may surface as max_tokens on the security
    reviewer's complete() call."""
    monkeypatch.setenv("PIPELINE_SECURITY_REVIEW_MAX_TOKENS", "8888")
    monkeypatch.setenv("PIPELINE_REVIEW_MAX_TOKENS", "7777")
    fake = _install_fake_backend(monkeypatch)
    p._run_security_reviewer(str(tmp_path / "wt"), "agent/some-branch")
    assert fake.kwargs is not None, "complete() was never called"
    assert "max_tokens" not in fake.kwargs, (
        f"max_tokens should be retired but was passed: {fake.kwargs!r}"
    )


def test_run_security_reviewer_fallback_var_also_no_effect(monkeypatch, tmp_path):
    """With the security-specific var unset, the fallback var must also have
    no effect -- the fallback chain is gone entirely."""
    monkeypatch.delenv("PIPELINE_SECURITY_REVIEW_MAX_TOKENS", raising=False)
    monkeypatch.setenv("PIPELINE_REVIEW_MAX_TOKENS", "7777")
    fake = _install_fake_backend(monkeypatch)
    p._run_security_reviewer(str(tmp_path / "wt"), "agent/some-branch")
    assert fake.kwargs is not None
    assert "max_tokens" not in fake.kwargs


def test_run_security_reviewer_preserves_other_kwargs(monkeypatch, tmp_path):
    """Removing max_tokens must not disturb the security reviewer's other
    call arguments."""
    monkeypatch.setenv("PIPELINE_SECURITY_REVIEW_MAX_TOKENS", "8888")
    fake = _install_fake_backend(monkeypatch)
    p._run_security_reviewer(str(tmp_path / "wt"), "agent/some-branch")
    assert fake.kwargs is not None
    for key in ("system", "model", "allowed_tools", "cwd", "cell_dir"):
        assert key in fake.kwargs, f"missing expected kwarg {key!r}: {fake.kwargs!r}"
    assert fake.kwargs["allowed_tools"] == "Bash,Read"
    assert fake.kwargs["cwd"] == str(tmp_path / "wt")


# --------------------------------------------------------------------------- #
# Source-level / doc-level assertions
# --------------------------------------------------------------------------- #

def _pipeline_review_py():
    """Absolute path to pipeline/review.py (the production file under test)."""
    # p is app.pipeline_mcp_server, a thin re-export shim; the real module
    # lives in the pipeline package at the repo root.
    return Path(p.__file__).resolve().parents[1] / "pipeline" / "review.py"


def _reference_md():
    """Absolute path to REFERENCE.md at the repo root."""
    return Path(p.__file__).resolve().parents[1] / "REFERENCE.md"


def test_review_py_no_longer_reads_max_tokens_env_vars():
    """pipeline/review.py must not reference either env var anywhere."""
    text = _pipeline_review_py().read_text()
    assert "PIPELINE_REVIEW_MAX_TOKENS" not in text, (
        "PIPELINE_REVIEW_MAX_TOKENS should be fully removed from pipeline/review.py"
    )
    assert "PIPELINE_SECURITY_REVIEW_MAX_TOKENS" not in text, (
        "PIPELINE_SECURITY_REVIEW_MAX_TOKENS should be fully removed from "
        "pipeline/review.py"
    )


def test_review_py_no_longer_passes_max_tokens_kwarg():
    """pipeline/review.py must not pass max_tokens= into any complete() call."""
    text = _pipeline_review_py().read_text()
    assert "max_tokens=" not in text, (
        "max_tokens= should no longer be passed to complete() in pipeline/review.py"
    )
    assert "max_tokens" not in text, (
        "no max_tokens reference of any kind should remain in pipeline/review.py"
    )


def test_reference_md_no_longer_documents_max_tokens_vars():
    """REFERENCE.md must not document either var as an output-cap flag."""
    text = _reference_md().read_text()
    assert "PIPELINE_REVIEW_MAX_TOKENS" not in text, (
        "REFERENCE.md should no longer document PIPELINE_REVIEW_MAX_TOKENS"
    )
    assert "PIPELINE_SECURITY_REVIEW_MAX_TOKENS" not in text, (
        "REFERENCE.md should no longer document PIPELINE_SECURITY_REVIEW_MAX_TOKENS"
    )