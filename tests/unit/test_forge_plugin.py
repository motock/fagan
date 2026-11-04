"""Tests for the forge plugin seam in ``pipeline/forge.py``.

A forge plugin is selected ONLY by the operator-controlled variable
``PIPELINE_FORGE_PLUGIN=module:attr``. The factory receives the repository root
and its adapter is validated against the :class:`Forge` protocol and cached per
repo root. A bad spec raises :class:`PluginError`; it never falls back to the
built-in GitHub adapter.
"""

import pytest

from app.plugin_loader import PluginError
from pipeline import forge as forge_mod
from pipeline.forge_github import GitHubForge

# The plugin spec points at this very module, so the loader imports the real
# test module and resolves ``make_forge`` from it.
PLUGIN_SPEC = f"{__name__}:make_forge"
BAD_PLUGIN_SPEC = f"{__name__}:make_no_merge"
INVALID_PLUGIN_SPEC = "not a valid spec"

# Every call the test-local factory receives, in order.
_CALLS: list[str] = []


class _StubForge:
    """A forge adapter that records the repo root it was built for."""

    def __init__(self, repo_root):
        self.repo_root = repo_root

    def open_pr(self, worktree, branch, title, body):
        return "handle"

    def update_pr_title(self, worktree, branch, title):
        return None

    def comment_pr(self, worktree, body):
        return None

    def merge_pr(self, worktree, branch, subject):
        return subject

    def pr_title(self, worktree, branch):
        return ""

    def pr_state(self, handle):
        return "UNKNOWN"

    def checks_status(self, branch, sha):
        return {}

    def has_ci_config(self, repo_root):
        return False

    def checks_snapshot(self, branch, sha):
        return forge_mod.CISnapshot()

    def failure_excerpt(self, branch, sha):
        return None

    def rerun_failed(self, branch, sha):
        return None

    def prepare_merge(self, worktree, branch):
        return None


class _NoMergeForge:
    """An adapter that is missing ``merge_pr`` (everything else is present)."""

    def open_pr(self, worktree, branch, title, body):
        return "handle"

    def update_pr_title(self, worktree, branch, title):
        return None

    def comment_pr(self, worktree, body):
        return None


def make_forge(repo_root):
    """The plugin factory under test: records the root and builds an adapter."""
    _CALLS.append(repo_root)
    return _StubForge(repo_root)


def make_no_merge(repo_root):
    """A plugin factory returning an adapter that fails validation."""
    return _NoMergeForge()


@pytest.fixture(autouse=True)
def _clear_forge_cache():
    """Each test starts with a cold adapter cache and an empty call log."""
    forge_mod._CACHE.clear()
    _CALLS.clear()
    yield
    forge_mod._CACHE.clear()
    _CALLS.clear()


def _set_forge_env(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PIPELINE_FORGE", raising=False)
    else:
        monkeypatch.setenv("PIPELINE_FORGE", value)


def _set_plugin(monkeypatch, spec):
    if spec is None:
        monkeypatch.delenv("PIPELINE_FORGE_PLUGIN", raising=False)
    else:
        monkeypatch.setenv("PIPELINE_FORGE_PLUGIN", spec)


# ---------------------------------------------------------------------------
# Unset plugin: unchanged built-in behaviour
# ---------------------------------------------------------------------------

def test_unset_plugin_returns_builtin_github_adapter(monkeypatch):
    _set_forge_env(monkeypatch, "github")
    _set_plugin(monkeypatch, None)

    adapter = forge_mod.get_forge("/repos/alpha")

    assert isinstance(adapter, GitHubForge)
    # No plugin selected, so no plugin factory runs.
    assert _CALLS == []


# ---------------------------------------------------------------------------
# Plugin selected: factory receives the repo root
# ---------------------------------------------------------------------------

def test_plugin_factory_receives_repo_root_and_adapter_is_returned(monkeypatch):
    _set_forge_env(monkeypatch, None)
    _set_plugin(monkeypatch, PLUGIN_SPEC)

    adapter = forge_mod.get_forge("/repos/alpha")

    assert isinstance(adapter, _StubForge)
    assert adapter.repo_root == "/repos/alpha"
    assert _CALLS == ["/repos/alpha"]


def test_same_root_returns_cached_instance_and_calls_factory_once(monkeypatch):
    _set_forge_env(monkeypatch, None)
    _set_plugin(monkeypatch, PLUGIN_SPEC)

    first = forge_mod.get_forge("/repos/alpha")
    second = forge_mod.get_forge("/repos/alpha")

    assert first is second
    assert _CALLS == ["/repos/alpha"]
    assert forge_mod._CACHE["/repos/alpha"] is first


def test_distinct_roots_get_distinct_instances(monkeypatch):
    _set_forge_env(monkeypatch, None)
    _set_plugin(monkeypatch, PLUGIN_SPEC)

    alpha = forge_mod.get_forge("/repos/alpha")
    beta = forge_mod.get_forge("/repos/beta")

    assert alpha is not beta
    assert _CALLS == ["/repos/alpha", "/repos/beta"]
    assert forge_mod._CACHE == {"/repos/alpha": alpha, "/repos/beta": beta}


# ---------------------------------------------------------------------------
# Conflict with an explicit PIPELINE_FORGE
# ---------------------------------------------------------------------------

def test_explicit_forge_conflicts_with_plugin(monkeypatch):
    _set_forge_env(monkeypatch, "gitlab")
    _set_plugin(monkeypatch, PLUGIN_SPEC)

    with pytest.raises(PluginError, match="conflicts"):
        forge_mod.get_forge("/repos/alpha")

    assert _CALLS == []


def test_auto_forge_does_not_conflict_with_plugin(monkeypatch):
    _set_forge_env(monkeypatch, "auto")
    _set_plugin(monkeypatch, PLUGIN_SPEC)

    adapter = forge_mod.get_forge("/repos/alpha")

    assert isinstance(adapter, _StubForge)
    assert _CALLS == ["/repos/alpha"]


# ---------------------------------------------------------------------------
# Validation: a plugin adapter must satisfy the Forge protocol
# ---------------------------------------------------------------------------

def test_adapter_missing_method_raises_and_is_not_cached(monkeypatch):
    _set_forge_env(monkeypatch, None)
    _set_plugin(monkeypatch, BAD_PLUGIN_SPEC)

    with pytest.raises(PluginError, match="merge_pr"):
        forge_mod.get_forge("/repos/alpha")

    # Nothing invalid is cached, so the next call fails the same way instead
    # of returning a half-supported adapter.
    with pytest.raises(PluginError, match="merge_pr"):
        forge_mod.get_forge("/repos/alpha")

    assert "/repos/alpha" not in forge_mod._CACHE


# ---------------------------------------------------------------------------
# A bad spec fails closed (never the built-in GitHub adapter)
# ---------------------------------------------------------------------------

def test_invalid_spec_raises_and_does_not_fall_back(monkeypatch):
    _set_forge_env(monkeypatch, None)
    _set_plugin(monkeypatch, INVALID_PLUGIN_SPEC)

    with pytest.raises(PluginError):
        adapter = forge_mod.get_forge("/repos/alpha")
        assert not isinstance(adapter, GitHubForge)

    assert forge_mod._CACHE == {}
    assert _CALLS == []
