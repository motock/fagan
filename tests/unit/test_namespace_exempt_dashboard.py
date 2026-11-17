"""Tests for the dashboard's namespace-exempt extension paths.

The dashboard binds a namespace to every request that carries tenant data
(the ``/api/`` and ``/mcp/`` surfaces). An operator may list extension paths
that run WITHOUT a bound namespace: ``PIPELINE_TENANT_DATA_EXEMPT_PATHS``.
Such a path still has to authenticate -- only the namespace binding is
skipped -- so the credential gate keeps answering 401 without a key, and a
path that is merely a prefix-neighbour of a listed path (or any other
``/api/`` path) is still refused with 403 ``namespace_error`` when the
resolver reaches no namespace.

A malformed exempt list is refused at import: the app must not start with a
half-read list, so ``import app.dashboard`` fails loudly.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import pipeline.namespace_resolver as resolver_module
from app import auth
from app import dashboard as d
from app.plugin_loader import ALLOWLIST_ENV
from pipeline.namespace_resolver import NAMESPACE_RESOLVER_PLUGIN_ENV
from tests.unit._dashboard_helpers import client, plan_dir

__all__ = ["client", "plan_dir"]

#: The reason token a refused namespace answers with.
NAMESPACE_ERROR = "namespace_error"

#: The environment variable the operator lists exempt paths in.
EXEMPT_ENV = "PIPELINE_TENANT_DATA_EXEMPT_PATHS"

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolate_resolver_plugins(monkeypatch):
    """Keep the process-wide namespace-resolver plugin cache out of the tests.

    The active resolver is cached for the life of the process, and the cache
    is consulted before the environment, so a plugin installed by one test
    would otherwise answer the next test's requests.
    """
    monkeypatch.delenv(NAMESPACE_RESOLVER_PLUGIN_ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    monkeypatch.setattr(resolver_module, "_PLUGIN_RESOLVERS", {})
    yield


def _install_resolver_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> str:
    """Write *source* as a fresh module, select it as the resolver, return its name.

    The module name is unique per call, so ``sys.modules`` never hands a later
    test an earlier test's stub. The allowlist names the module: the resolver
    seam is security-sensitive, so a plugin is refused without one.
    """
    name = "probe_nsr_" + uuid.uuid4().hex
    (tmp_path / f"{name}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(NAMESPACE_RESOLVER_PLUGIN_ENV, f"{name}:build")
    monkeypatch.setenv(ALLOWLIST_ENV, name)
    return name


def _stub_resolver_source(accesses_body: str) -> str:
    """Return plugin source whose ``accesses`` method body is *accesses_body*.

    The body is spliced into the source verbatim, so it may use braces freely:
    only the template text is subject to f-string rules.
    """
    return f'''
from pipeline.namespace_resolver import NamespaceAccess


class StubResolver:
    def accesses(self, principal):
        {accesses_body}


def build():
    return StubResolver()
'''


def _api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point the dashboard gate at a throwaway key file and return the secret.

    The real repository key file is never read or written by these tests.
    """
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return auth.get_or_create_api_key()


def _exempt_resolver(monkeypatch, tmp_path):
    """Install a resolver that reaches no namespace and list one exempt path."""
    _install_resolver_plugin(tmp_path, monkeypatch, _stub_resolver_source("return ()"))
    monkeypatch.setattr(d, "_EXEMPT_PATHS", ("/api/ext/probe",))


def test_exempt_path_is_served_without_a_bound_namespace(
    client, plan_dir, monkeypatch, tmp_path
):
    """A listed extension path skips the namespace binding: it answers 404.

    No route matches ``/api/ext/probe``, so the request is served (404) rather
    than refused by the namespace wrapper (403).
    """
    _exempt_resolver(monkeypatch, tmp_path)
    key = _api_key(tmp_path, monkeypatch)

    response = client.get("/api/ext/probe", headers={"x-pipeline-api-key": key})

    assert response.status_code == 404


def test_exempt_path_below_the_listed_path_is_served_too(
    client, plan_dir, monkeypatch, tmp_path
):
    """A path below a listed path at a ``/`` boundary is exempt as well."""
    _exempt_resolver(monkeypatch, tmp_path)
    key = _api_key(tmp_path, monkeypatch)

    response = client.get("/api/ext/probe/me", headers={"x-pipeline-api-key": key})

    assert response.status_code == 404


def test_exempt_path_still_requires_the_credential(
    client, plan_dir, monkeypatch, tmp_path
):
    """Skipping the namespace binding does not skip authentication."""
    _exempt_resolver(monkeypatch, tmp_path)
    _api_key(tmp_path, monkeypatch)

    response = client.get("/api/ext/probe")

    assert response.status_code == 401
    assert response.json()["detail"] == "unauthorized"


def test_a_path_next_to_the_exempt_path_is_still_refused(
    client, plan_dir, monkeypatch, tmp_path
):
    """``/api/ext/probeX`` is not below ``/api/ext/probe``: it stays bound."""
    _exempt_resolver(monkeypatch, tmp_path)
    key = _api_key(tmp_path, monkeypatch)

    response = client.get("/api/ext/probeX", headers={"x-pipeline-api-key": key})

    assert response.status_code == 403
    assert response.json()["detail"] == NAMESPACE_ERROR


def test_other_api_paths_are_still_refused(
    client, plan_dir, monkeypatch, tmp_path
):
    """Only the listed path is exempt: every other ``/api/`` path is bound."""
    _exempt_resolver(monkeypatch, tmp_path)
    key = _api_key(tmp_path, monkeypatch)

    response = client.get("/api/health", headers={"x-pipeline-api-key": key})

    assert response.status_code == 403
    assert response.json()["detail"] == NAMESPACE_ERROR


def test_no_exempt_paths_means_no_exemption(
    client, plan_dir, monkeypatch, tmp_path
):
    """With nothing listed, the path is bound like any other ``/api/`` path."""
    _install_resolver_plugin(tmp_path, monkeypatch, _stub_resolver_source("return ()"))
    monkeypatch.setattr(d, "_EXEMPT_PATHS", ())
    key = _api_key(tmp_path, monkeypatch)

    response = client.get("/api/ext/probe", headers={"x-pipeline-api-key": key})

    assert response.status_code == 403
    assert response.json()["detail"] == NAMESPACE_ERROR


def test_serves_tenant_data_answers_false_for_an_exempt_path(monkeypatch, tmp_path):
    """The tenant-data decision follows the exempt list."""
    _exempt_resolver(monkeypatch, tmp_path)

    assert d._serves_tenant_data("/api/ext/probe") is False
    assert d._serves_tenant_data("/api/plans") is True


def test_the_tenant_data_surfaces_are_unchanged():
    """The API and MCP surfaces keep carrying tenant data."""
    assert "/api/" in d.TENANT_DATA_PATHS
    assert "/mcp/" in d.TENANT_DATA_PATHS


def test_a_malformed_exempt_list_refuses_the_import(tmp_path):
    """A listed path that is not an extension path stops the app at import."""
    env = dict(os.environ)
    env[EXEMPT_ENV] = "/api/plans"

    result = subprocess.run(
        [sys.executable, "-c", "import app.dashboard"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode != 0
    assert EXEMPT_ENV in result.stderr


def test_a_valid_exempt_list_lets_the_app_start(tmp_path):
    """The positive control: a listed extension path starts the app."""
    env = dict(os.environ)
    env[EXEMPT_ENV] = "/api/ext/probe"

    result = subprocess.run(
        [sys.executable, "-c", "import app.dashboard"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
