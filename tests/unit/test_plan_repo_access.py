"""A plan naming a repository is checked against the access hook before any clone.

``remote_scope`` saves, decomposes and ingests a plan that names a ``repo_url``.
Before this check a plan for a repository the namespace could not dispatch to
was cloned at save time and refused only at dispatch. Now the repository access
hook (``PIPELINE_REPO_ACCESS_PLUGIN``) is asked for the ``dispatch`` action
first, and a refusal runs no clone, fetch or filesystem work.

The hook is stubbed through the real plugin loader. Only ``repo_clone.ensure_clone``
(the git clone) is replaced, by a recorder.
"""

from __future__ import annotations

import importlib
import os
import uuid
from pathlib import Path

import pytest

from app.plugin_loader import ALLOWLIST_ENV
from pipeline import access_hooks, remote_scope, repo_clone, repo_read_access
from pipeline.access_hooks import ACCESS_HOOK_PLUGIN_ENV
from pipeline.actor import current_namespace

GRANTED_URL = "https://github.com/a/one"
UNGRANTED_URL = "https://github.com/b/two"
UNIDENTIFIED_URL = "https://github.com/only-one-segment"

PLUGIN_TEMPLATE = """\
from pipeline.access_hooks import AccessDecision

CALLS = []


class StubHook:
    def check(self, namespace, repo, action, risk):
        CALLS.append((namespace, repo, action, risk))
{body}


def build():
    return StubHook()
"""

#: Refuse ``dispatch`` only; allow every other action.
REFUSE_DISPATCH = """\
        if action == "dispatch":
            return AccessDecision(allowed=False, reason="repo_not_granted")
        return AccessDecision(allowed=True, reason="ok")"""

#: Allow everything; the hook is asked and admits.
ALLOW_ALL = """\
        return AccessDecision(allowed=True, reason="ok")"""

#: Refuse with free text that must never reach a refusal or an audit record.
REFUSE_WITH_FREE_TEXT = """\
        return AccessDecision(allowed=False, reason="Team Foo may not touch github.com/a/one")"""

#: The hook raises: any failure to consult must refuse.
RAISE = """\
        raise RuntimeError("hook exploded")"""


@pytest.fixture(autouse=True)
def _no_plugin_configured(monkeypatch):
    monkeypatch.delenv(ACCESS_HOOK_PLUGIN_ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    monkeypatch.delenv("PIPELINE_REPO_URL_HOSTS", raising=False)
    monkeypatch.delenv("PIPELINE_REPO_URL_ALLOW_FILE", raising=False)


@pytest.fixture(autouse=True)
def repos_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setenv("PIPELINE_REPOS_ROOT", str(root))
    return root


@pytest.fixture(autouse=True)
def audit_records(monkeypatch):
    """Capture host audit appends so no test writes the real audit chain."""
    records: list[dict] = []
    monkeypatch.setattr(
        access_hooks, "append_host_audit", lambda **kwargs: records.append(kwargs)
    )
    return records


class CloneRecorder:
    """Stands in for ``repo_clone.ensure_clone``; records every clone attempt."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url, environ=None, timeout=600):
        self.calls.append(url)
        return repo_clone.managed_path(url)


@pytest.fixture
def clone(monkeypatch) -> CloneRecorder:
    recorder = CloneRecorder()
    monkeypatch.setattr(repo_clone, "ensure_clone", recorder)
    return recorder


def _select_hook(tmp_path: Path, monkeypatch, body: str) -> str:
    """Write a hook module with ``body`` as its ``check``, select it, return its name."""
    name = "plan_repo_access_" + uuid.uuid4().hex
    (tmp_path / f"{name}.py").write_text(
        PLUGIN_TEMPLATE.replace("{body}", body), encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(ACCESS_HOOK_PLUGIN_ENV, f"{name}:build")
    monkeypatch.setenv(ALLOWLIST_ENV, name)
    return name


def _hook_calls(name: str) -> list:
    return importlib.import_module(name).CALLS


def _resolve(url: str) -> str | None:
    """Return the error of ``resolve_repo_url`` for a plan naming ``url``, or None."""
    result = remote_scope.resolve_repo_url({"repo_url": url})
    return None if result is None else result["error"]


def _clone_root(url: str) -> str | None:
    """Return the error of ``clone_root_for`` for ``url``, or None."""
    _, err = remote_scope.clone_root_for(url)
    return None if err is None else err["error"]


def _refresh(url: str) -> str | None:
    """Return the error of ``refresh_plan_clone`` for a plan naming ``url``, or None."""
    result = remote_scope.refresh_plan_clone({"repo_url": url})
    return None if result is None else result["error"]


ENTRY_POINTS = [
    pytest.param(_resolve, id="save"),
    pytest.param(_clone_root, id="decompose"),
    pytest.param(_refresh, id="ingest"),
]


# --- no hook configured: behaviour unchanged --------------------------------------


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_with_no_hook_configured_the_plan_repository_is_cloned_once(entry, clone):
    assert entry(GRANTED_URL) is None
    assert clone.calls == [GRANTED_URL]


def test_with_no_hook_configured_save_points_repo_root_at_the_clone(clone):
    plan = {"repo_url": GRANTED_URL}
    assert remote_scope.resolve_repo_url(plan) is None
    assert plan["repo_root"] == str(repo_clone.managed_path(GRANTED_URL))


def test_with_no_hook_configured_a_malformed_url_keeps_the_clone_error_text():
    error = _resolve(UNIDENTIFIED_URL)
    assert error is not None
    assert not error.startswith("access refused")


# --- hook admits: the clone still runs, once -----------------------------------


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_with_a_hook_that_admits_dispatch_the_repository_is_cloned_once(
    entry, tmp_path, monkeypatch, clone
):
    name = _select_hook(tmp_path, monkeypatch, ALLOW_ALL)
    assert entry(GRANTED_URL) is None
    assert clone.calls == [GRANTED_URL]
    assert _hook_calls(name) == [(current_namespace(), "github.com/a/one", "dispatch", "low")]


# --- hook refuses dispatch: refusal, zero clones, one audit record ----------------


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_refused_dispatch_returns_the_refusal_and_never_clones(
    entry, tmp_path, monkeypatch, clone, audit_records
):
    _select_hook(tmp_path, monkeypatch, REFUSE_DISPATCH)
    assert entry(UNGRANTED_URL) == "access refused: repo_not_granted"
    assert clone.calls == []


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_refused_dispatch_appends_one_audit_record_with_only_the_reason(
    entry, tmp_path, monkeypatch, clone, audit_records
):
    _select_hook(tmp_path, monkeypatch, REFUSE_DISPATCH)
    entry(UNGRANTED_URL)
    assert audit_records == [
        {
            "action": "repo_access_refused",
            "outcome": "refused",
            "detail": {"access_action": "dispatch", "reason": "repo_not_granted"},
        }
    ]


def test_a_refused_save_leaves_the_plan_without_a_repo_root(
    tmp_path, monkeypatch, clone
):
    _select_hook(tmp_path, monkeypatch, REFUSE_DISPATCH)
    plan = {"repo_url": UNGRANTED_URL}
    remote_scope.resolve_repo_url(plan)
    assert "repo_root" not in plan


# --- hook raises or returns free text --------------------------------------------


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_hook_that_raises_refuses_without_cloning(
    entry, tmp_path, monkeypatch, clone, audit_records
):
    _select_hook(tmp_path, monkeypatch, RAISE)
    assert entry(GRANTED_URL) == "access refused: hook_error"
    assert clone.calls == []
    assert audit_records[0]["detail"] == {"access_action": "dispatch", "reason": "hook_error"}


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_refusal_text_carries_neither_the_url_nor_the_hook_free_text(
    entry, tmp_path, monkeypatch, clone, audit_records
):
    _select_hook(tmp_path, monkeypatch, REFUSE_WITH_FREE_TEXT)
    error = entry(UNGRANTED_URL)
    assert error == "access refused: unspecified"
    assert "github.com" not in error
    assert "Team Foo" not in error
    assert "Team Foo" not in str(audit_records)
    assert "github.com" not in str(audit_records)


# --- a repository identity that cannot be derived --------------------------------


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_an_unidentified_repository_is_refused_before_the_hook_and_any_clone(
    entry, tmp_path, monkeypatch, clone, audit_records
):
    name = _select_hook(tmp_path, monkeypatch, ALLOW_ALL)
    assert entry(UNIDENTIFIED_URL) == "access refused: repository_unidentified"
    assert clone.calls == []
    assert _hook_calls(name) == []
    assert audit_records == [
        {
            "action": "repo_access_refused",
            "outcome": "refused",
            "detail": {"access_action": "dispatch", "reason": "repository_unidentified"},
        }
    ]


# --- the read path keeps its first-read clone -----------------------------------


def test_a_read_grant_still_clones_on_first_read_when_dispatch_is_refused(
    tmp_path, monkeypatch, clone
):
    _select_hook(tmp_path, monkeypatch, REFUSE_DISPATCH)
    kwargs = repo_read_access.read_root_kwargs(GRANTED_URL)
    assert kwargs["root"].endswith(os.path.join("github.com", "a", "one"))
    assert clone.calls == [GRANTED_URL]
