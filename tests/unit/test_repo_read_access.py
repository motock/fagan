"""A repository read is allowed only when the caller's namespace may read it.

The three ``GET /api/workspace/...`` routes read the managed clone a
``repo_url`` names. Before this check they consulted only the host allow-list
and root containment, so a namespace could read a clone another namespace had
cloned. The repository access hook (``PIPELINE_REPO_ACCESS_PLUGIN``) now answers
``action="read"`` before any read; a refusal answers 403 with a fixed detail.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import pipeline.namespace_access as access_module
from app import dashboard as d
from app.identity import ROLE_AUTHOR
from pipeline import access_hooks
from pipeline.access_hooks import ACCESS_HOOK_PLUGIN_ENV, AccessDecision
from pipeline.namespace_access import NAMESPACE_HEADER
from pipeline.namespace_resolver import NamespaceAccess
from pipeline.repo_read_access import read_refusal

NAMESPACE = "team-a"
ALLOWED_REPO = "github.com/a/one"
ALLOWED_URL = "https://github.com/a/one"
OTHER_URL = "https://github.com/b/two"
OTHER_REPO = "github.com/b/two"
REFUSED_DETAIL = "repository access refused"
NOT_ALLOWED_DETAIL = "repo_url is not an allowed repository URL"

READ_ROUTES = [
    ("/api/workspace/file", {"path": "README.md"}),
    ("/api/workspace/files", {"path": ""}),
    ("/api/workspace/search", {"pattern": "needle"}),
]


class StubHook:
    """Stands in for the access hook object; records every consultation."""

    def __init__(self, *, allowed=(ALLOWED_REPO,), error=None):
        self.allowed = allowed
        self.error = error
        self.calls: list[dict] = []

    def check(self, namespace, repo, action, risk):
        self.calls.append(
            {"namespace": namespace, "repo": repo, "action": action, "risk": risk}
        )
        if self.error is not None:
            raise self.error
        return AccessDecision(allowed=repo in self.allowed, reason="repo_not_granted")


@pytest.fixture(autouse=True)
def _no_plugin_env(monkeypatch):
    monkeypatch.delenv(ACCESS_HOOK_PLUGIN_ENV, raising=False)


@pytest.fixture(autouse=True)
def audit_records(monkeypatch):
    """Capture host audit appends so no test writes the real audit chain."""
    records: list[dict] = []
    monkeypatch.setattr(
        access_hooks, "append_host_audit", lambda **kwargs: records.append(kwargs)
    )
    return records


@pytest.fixture
def repos_root(tmp_path, monkeypatch):
    """A managed-clone root holding a clone for each of the two repositories."""
    root = tmp_path / "repos"
    for owner, name, text in (("a", "one", "hello one\n"), ("b", "two", "hello two\n")):
        clone = root / "github.com" / owner / name
        (clone / "src").mkdir(parents=True)
        (clone / "README.md").write_text(text, encoding="utf-8")
        (clone / "src" / "app.py").write_text("needle = 1\n", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_REPOS_ROOT", str(root))
    monkeypatch.delenv("PIPELINE_REPO_URL_HOSTS", raising=False)
    monkeypatch.delenv("PIPELINE_REPO_URL_ALLOW_FILE", raising=False)
    return root


@pytest.fixture
def client():
    return TestClient(d.app)


@pytest.fixture
def hook(monkeypatch):
    stub = StubHook()
    monkeypatch.setattr(access_hooks, "_hook", lambda: stub)
    return stub


class _Resolver:
    """Resolver double: the caller may reach each of the given namespaces."""

    def __init__(self, *namespaces: str) -> None:
        self._namespaces = namespaces

    def accesses(self, principal):
        return tuple(
            NamespaceAccess(name, frozenset({ROLE_AUTHOR})) for name in self._namespaces
        )


@pytest.fixture
def namespaces(monkeypatch):
    """Two reachable namespaces, so the request header names the one bound."""
    monkeypatch.setattr(
        access_module,
        "active_namespace_resolver",
        lambda: _Resolver(NAMESPACE, "team-b"),
    )


@pytest.fixture
def service_reads(monkeypatch):
    """Record every service read; each one answers a successful file read."""
    calls: list[dict] = []

    def fake_read(path, **kwargs):
        calls.append({"path": path, **kwargs})
        return {"ok": True, "path": path, "content": "served\n"}

    monkeypatch.setattr(d._service, "read_workspace_file", fake_read)
    return calls


@pytest.mark.parametrize("route, params", READ_ROUTES)
def test_should_refuse_a_read_of_a_repository_the_namespace_may_not_read(
    client, repos_root, hook, route, params
):
    response = client.get(route, params={**params, "repo_url": OTHER_URL})
    assert response.status_code == 403


def test_should_read_an_allowed_repository_as_before(client, repos_root, hook):
    response = client.get(
        "/api/workspace/file", params={"path": "README.md", "repo_url": ALLOWED_URL}
    )
    assert response.status_code == 200
    assert response.json()["content"] == "hello one\n"


def test_should_read_a_repository_when_no_access_hook_is_configured(client, repos_root):
    response = client.get(
        "/api/workspace/file", params={"path": "README.md", "repo_url": OTHER_URL}
    )
    assert response.status_code == 200
    assert response.json()["content"] == "hello two\n"


def test_should_refuse_a_read_when_the_hook_raises(client, repos_root, monkeypatch, service_reads):
    monkeypatch.setattr(access_hooks, "_hook", lambda: StubHook(error=RuntimeError("boom")))
    response = client.get(
        "/api/workspace/file", params={"path": "README.md", "repo_url": OTHER_URL}
    )
    assert response.status_code == 403
    assert service_reads == []


@pytest.mark.parametrize(
    "answer",
    [
        None,
        {"allowed": True, "reason": "ok"},
        AccessDecision(allowed="yes", reason="ok"),
    ],
    ids=["none", "mapping", "non_bool_allowed"],
)
def test_should_refuse_a_read_when_the_hook_returns_a_malformed_decision(
    client, repos_root, monkeypatch, service_reads, answer
):
    stub = StubHook()
    stub.check = lambda namespace, repo, action, risk: answer
    monkeypatch.setattr(access_hooks, "_hook", lambda: stub)
    response = client.get(
        "/api/workspace/file", params={"path": "README.md", "repo_url": ALLOWED_URL}
    )
    assert response.status_code == 403
    assert service_reads == []


def test_should_answer_a_refused_read_with_the_fixed_detail(client, repos_root, hook):
    response = client.get(
        "/api/workspace/file", params={"path": "README.md", "repo_url": OTHER_URL}
    )
    assert response.json() == {"detail": REFUSED_DETAIL}


def test_should_keep_the_url_and_path_out_of_a_refused_read_body(client, repos_root, hook):
    response = client.get(
        "/api/workspace/file", params={"path": "README.md", "repo_url": OTHER_URL}
    )
    assert OTHER_URL not in response.text
    assert OTHER_REPO not in response.text
    assert "README.md" not in response.text


def test_should_ask_the_hook_about_a_read_in_the_bound_namespace(
    client, repos_root, hook, namespaces
):
    client.get(
        "/api/workspace/file",
        params={"path": "README.md", "repo_url": OTHER_URL},
        headers={NAMESPACE_HEADER: NAMESPACE},
    )
    assert hook.calls == [
        {"namespace": NAMESPACE, "repo": OTHER_REPO, "action": "read", "risk": "low"}
    ]


def test_should_not_consult_the_hook_for_a_read_with_no_repo_url(
    client, hook, service_reads
):
    response = client.get("/api/workspace/file", params={"path": "README.md"})
    assert response.status_code == 200
    assert hook.calls == []


def test_should_refuse_a_repo_url_outside_the_allow_list_before_the_hook(
    client, repos_root, hook
):
    response = client.get(
        "/api/workspace/file",
        params={"path": "README.md", "repo_url": "https://evil.example/x/y"},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": NOT_ALLOWED_DETAIL}
    assert hook.calls == []


def test_should_write_one_refusal_record_without_the_url(
    client, repos_root, hook, audit_records
):
    client.get(
        "/api/workspace/file", params={"path": "README.md", "repo_url": OTHER_URL}
    )
    assert len(audit_records) == 1
    record = audit_records[0]
    assert record["action"] == "repo_access_refused"
    assert record["detail"] == {"access_action": "read", "reason": "repo_not_granted"}
    assert "b/two" not in str(record)
    assert "https://" not in str(record)


def test_should_refuse_a_read_whose_repository_identity_cannot_be_derived(
    audit_records,
):
    assert read_refusal("https://github.com/only-one-segment") == "repository_unidentified"
    assert len(audit_records) == 1
