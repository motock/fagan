"""Tests for the forge resolution seam in ``pipeline/forge.py``.

``resolve_forge_name`` is the pure resolution seam: it maps ``PIPELINE_FORGE``
(and, under ``auto``, the origin remote's hostname) to an adapter name.
``get_forge`` turns that name into a cached adapter instance.

The remote probe is the only external boundary here, so these tests patch
``pipeline.forge._remote_url`` (the thin wrapper around ``git remote get-url``)
rather than spawning git; one test pins the real argv.
"""

import logging
import subprocess

import pytest

from pipeline import forge as forge_mod


@pytest.fixture(autouse=True)
def _clear_forge_cache():
    """Each test starts with a cold adapter cache."""
    forge_mod._CACHE.clear()
    yield
    forge_mod._CACHE.clear()


def _set_forge_env(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PIPELINE_FORGE", raising=False)
    else:
        monkeypatch.setenv("PIPELINE_FORGE", value)


def _patch_remote(monkeypatch, url):
    """Replace the git remote probe; returns the list of probed roots."""
    probed = []

    def _fake_remote_url(repo_root):
        probed.append(repo_root)
        return url

    monkeypatch.setattr(forge_mod, "_remote_url", _fake_remote_url)
    return probed


# ---------------------------------------------------------------------------
# Explicit PIPELINE_FORGE values
# ---------------------------------------------------------------------------

def test_explicit_github_resolves_github(monkeypatch):
    _set_forge_env(monkeypatch, "github")
    probed = _patch_remote(monkeypatch, "git@gitlab.com:o/r.git")

    assert forge_mod.resolve_forge_name("/repo") == "github"
    # An explicit value must never pay for a remote probe.
    assert probed == []


def test_explicit_gitlab_resolves_gitlab(monkeypatch):
    """Resolution names gitlab even before S4 registers its adapter."""
    _set_forge_env(monkeypatch, "gitlab")
    probed = _patch_remote(monkeypatch, "git@github.com:o/r.git")

    assert forge_mod.resolve_forge_name("/repo") == "gitlab"
    assert probed == []


def test_unknown_env_value_raises_value_error(monkeypatch):
    _set_forge_env(monkeypatch, "bitbucket")
    _patch_remote(monkeypatch, "git@github.com:o/r.git")

    with pytest.raises(ValueError) as excinfo:
        forge_mod.resolve_forge_name("/repo")

    message = str(excinfo.value)
    assert "bitbucket" in message
    # The allowed set is named, mirroring get_ticket_provider()'s style.
    assert "github" in message and "gitlab" in message and "auto" in message


# ---------------------------------------------------------------------------
# auto: remote hostname mapping
# ---------------------------------------------------------------------------

def test_auto_maps_gitlab_remote_to_gitlab(monkeypatch):
    _set_forge_env(monkeypatch, None)
    _patch_remote(monkeypatch, "git@gitlab.com:org/repo.git")

    assert forge_mod.resolve_forge_name("/repo") == "gitlab"


def test_auto_maps_github_remote_to_github(monkeypatch):
    _set_forge_env(monkeypatch, None)
    _patch_remote(monkeypatch, "https://github.com/org/repo.git")

    assert forge_mod.resolve_forge_name("/repo") == "github"


def test_auto_unknown_host_falls_back_to_github_and_warns(monkeypatch, caplog):
    _set_forge_env(monkeypatch, None)
    _patch_remote(monkeypatch, "https://git.example.com/org/repo.git")

    with caplog.at_level(logging.WARNING, logger="pipeline.forge"):
        assert forge_mod.resolve_forge_name("/repo") == "github"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "git.example.com" in warnings[0].getMessage()


def test_auto_missing_remote_falls_back_to_github_and_warns(monkeypatch, caplog):
    _set_forge_env(monkeypatch, None)
    _patch_remote(monkeypatch, None)

    with caplog.at_level(logging.WARNING, logger="pipeline.forge"):
        assert forge_mod.resolve_forge_name("/repo") == "github"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]


def test_auto_probe_uses_git_remote_get_url_argv(monkeypatch):
    """The probe is `git -C <repo_root> remote get-url origin`."""
    _set_forge_env(monkeypatch, None)
    seen = []

    def _fake_run(cmd, **kwargs):
        seen.append((list(cmd), kwargs))
        return subprocess.CompletedProcess(
            cmd, 0, stdout="git@github.com:org/repo.git\n", stderr=""
        )

    # The probe is bound at import time (``forge._RUN``) so that tests which
    # patch ``subprocess.run`` to observe the PR verbs do not count it.
    monkeypatch.setattr(forge_mod, "_RUN", _fake_run)

    assert forge_mod.resolve_forge_name("/repo") == "github"
    assert seen[0][0] == ["git", "-C", "/repo", "remote", "get-url", "origin"]


# ---------------------------------------------------------------------------
# get_forge: registration + per-repo_root cache
# ---------------------------------------------------------------------------

def test_get_forge_returns_same_instance_for_same_root(monkeypatch):
    _set_forge_env(monkeypatch, "github")
    _patch_remote(monkeypatch, "git@github.com:o/r.git")

    first = forge_mod.get_forge("/repo")
    second = forge_mod.get_forge("/repo")

    assert first is second


def test_cache_is_per_repo_root(monkeypatch):
    """Distinct roots get distinct adapters; one root probes git only once."""
    _set_forge_env(monkeypatch, None)
    remotes = {
        "/repo-a": "git@github.com:o/a.git",
        "/repo-b": "git@github.com:o/b.git",
    }
    probed = []

    def _fake_remote_url(repo_root):
        probed.append(repo_root)
        return remotes[repo_root]

    monkeypatch.setattr(forge_mod, "_remote_url", _fake_remote_url)

    a1 = forge_mod.get_forge("/repo-a")
    b1 = forge_mod.get_forge("/repo-b")
    a2 = forge_mod.get_forge("/repo-a")

    assert a1 is not b1
    assert a1 is a2
    # The remote is read at most once per repo_root per process.
    assert probed == ["/repo-a", "/repo-b"]


def test_gitlab_forge_is_registered(monkeypatch):
    """S4 registers the gitlab adapter, so resolution yields an adapter."""
    from pipeline.forge_gitlab import GitLabForge

    _set_forge_env(monkeypatch, "gitlab")
    _patch_remote(monkeypatch, "git@gitlab.com:o/r.git")

    assert isinstance(forge_mod.get_forge("/repo"), GitLabForge)


def test_unregistered_forge_raises(monkeypatch):
    """A resolved name with no registered adapter still raises ValueError."""
    _set_forge_env(monkeypatch, "gitlab")
    _patch_remote(monkeypatch, "git@gitlab.com:o/r.git")
    # Simulate a name that resolves but has no adapter: the registration
    # table is non-empty, so _register_adapters() leaves it alone.
    monkeypatch.setattr(forge_mod, "_ADAPTERS", {"github": object})

    with pytest.raises(ValueError) as excinfo:
        forge_mod.get_forge("/repo")

    assert "gitlab" in str(excinfo.value)


# ---------------------------------------------------------------------------
# CI state constants
# ---------------------------------------------------------------------------

def test_ci_states_are_distinct_and_include_unreadable_and_none():
    assert forge_mod.UNREADABLE != forge_mod.NONE
    assert forge_mod.UNREADABLE in forge_mod.CI_STATES
    assert forge_mod.NONE in forge_mod.CI_STATES
    assert forge_mod.CI_STATES == frozenset(
        {"pass", "fail", "pending", "cancelled", "unreadable", "none"}
    )
