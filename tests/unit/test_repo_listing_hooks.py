"""The repository listing seam behind the ``list_repositories`` tool.

With ``PIPELINE_REPO_LISTING_PLUGIN`` unset the answer is the repositories of the
caller's visible plans. With it set, the plugin answers for the bound namespace,
and every answer is checked before it is returned: a failure fails closed.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.identity import Principal
from app.plugin_loader import ALLOWLIST_ENV
from pipeline.actor import bind_namespace, bind_principal
from pipeline.repo_listing_hooks import (
    REPO_LISTING_PLUGIN_ENV,
    UNAVAILABLE_REASON,
    list_repositories_result,
)

ALICE = Principal(subject="https://idp#alice", auth_method="oidc")
BOB = Principal(subject="https://idp#bob", auth_method="oidc")
NAMESPACE = "team-a"

_TEMPLATE = """\
CALLS = []


class Hook:
    def list_repositories(self, namespace):
        CALLS.append(namespace)
@@BODY@@


def build():
    return Hook()
"""


class _Records:
    def __init__(self, plans: dict[str, dict[str, dict[str, Any]]]) -> None:
        self._plans = plans

    def list_plans(self, namespace: str) -> list[str]:
        return sorted(self._plans.get(namespace, {}))

    def get_manifest(self, namespace: str, plan_name: str) -> dict[str, Any] | None:
        return self._plans.get(namespace, {}).get(plan_name)


class _Store:
    def __init__(self, plans: dict[str, dict[str, dict[str, Any]]] | None = None) -> None:
        self._records = _Records(plans or {})

    def records(self) -> _Records:
        return self._records


def _plan(owner: str, repo_url: str) -> dict[str, Any]:
    return {"owner": owner, "repo_url": repo_url}


@pytest.fixture(autouse=True)
def _no_plugin_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REPO_LISTING_PLUGIN_ENV, raising=False)
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)


def _install_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> str:
    """Write a fresh plugin module whose ``list_repositories`` runs ``body``."""
    name = "repo_listing_" + uuid.uuid4().hex
    (tmp_path / f"{name}.py").write_text(
        _TEMPLATE.replace("@@BODY@@", body), encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(REPO_LISTING_PLUGIN_ENV, f"{name}:build")
    monkeypatch.setenv(ALLOWLIST_ENV, name)
    return name


def _plugin_calls(name: str) -> list[str]:
    return sys.modules[name].CALLS


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno == logging.WARNING]


# --- no plugin: the caller's visible plans -----------------------------------


def test_default_lists_the_distinct_repositories_sorted() -> None:
    store = _Store(
        {
            NAMESPACE: {
                "one": _plan(ALICE.subject, "https://github.com/acme/widgets"),
                "two": _plan(ALICE.subject, "https://github.com/acme/api"),
                "three": _plan(ALICE.subject, "https://github.com/acme/widgets"),
            }
        }
    )
    with bind_namespace(NAMESPACE), bind_principal(ALICE):
        result = list_repositories_result(store)
    assert result == {
        "ok": True,
        "repositories": [
            {"repo": "github.com/acme/api", "level": None, "state": None},
            {"repo": "github.com/acme/widgets", "level": None, "state": None},
        ],
    }


def test_default_omits_a_plan_the_caller_cannot_see() -> None:
    store = _Store(
        {
            NAMESPACE: {
                "mine": _plan(ALICE.subject, "https://github.com/acme/widgets"),
                "theirs": _plan(BOB.subject, "https://github.com/bob/secret"),
            }
        }
    )
    with bind_namespace(NAMESPACE), bind_principal(ALICE):
        result = list_repositories_result(store)
    assert [item["repo"] for item in result["repositories"]] == [
        "github.com/acme/widgets"
    ]


def test_default_reads_only_the_bound_namespace() -> None:
    store = _Store(
        {
            NAMESPACE: {"mine": _plan(ALICE.subject, "https://github.com/acme/widgets")},
            "team-b": {"other": _plan(ALICE.subject, "https://github.com/b/other")},
        }
    )
    with bind_namespace(NAMESPACE), bind_principal(ALICE):
        result = list_repositories_result(store)
    assert [item["repo"] for item in result["repositories"]] == [
        "github.com/acme/widgets"
    ]


def test_default_leaves_out_a_plan_whose_repository_is_a_local_path() -> None:
    store = _Store(
        {NAMESPACE: {"local": {"owner": ALICE.subject, "repo_root": "/srv/widgets"}}}
    )
    with bind_namespace(NAMESPACE), bind_principal(ALICE):
        result = list_repositories_result(store)
    assert result == {"ok": True, "repositories": []}


# --- plugin set: its answer for the bound namespace ---------------------------


def test_plugin_answer_is_returned_checked_and_sorted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = _install_plugin(
        tmp_path,
        monkeypatch,
        """\
        return [
            {"repo": "git.example.com/team/zeta", "level": "full", "state": "active"},
            {"repo": "git.example.com/team/alpha", "level": None, "state": None},
        ]""",
    )
    with bind_namespace(NAMESPACE):
        result = list_repositories_result(_Store())
    assert result == {
        "ok": True,
        "repositories": [
            {"repo": "git.example.com/team/alpha", "level": None, "state": None},
            {"repo": "git.example.com/team/zeta", "level": "full", "state": "active"},
        ],
    }
    assert _plugin_calls(name) == [NAMESPACE]


def test_plugin_is_given_the_bound_namespace_not_a_caller_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = _install_plugin(tmp_path, monkeypatch, "        return []")
    with bind_namespace("team-z"):
        list_repositories_result(_Store())
    assert _plugin_calls(name) == ["team-z"]


def test_plugin_extra_item_keys_are_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_plugin(
        tmp_path,
        monkeypatch,
        """\
        return [{"repo": "git.example.com/team/api", "token": "s3cr3t", "level": None, "state": None}]""",
    )
    with bind_namespace(NAMESPACE):
        result = list_repositories_result(_Store())
    assert result["repositories"] == [
        {"repo": "git.example.com/team/api", "level": None, "state": None}
    ]


# --- failures fail closed -----------------------------------------------------


def test_plugin_raising_answers_unavailable_with_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install_plugin(tmp_path, monkeypatch, '        raise RuntimeError("detail-xyz-42")')
    with bind_namespace(NAMESPACE):
        result = list_repositories_result(_Store())
    assert result["ok"] is False
    assert result["reason"] == UNAVAILABLE_REASON
    assert len(_warnings(caplog)) == 1
    assert _warnings(caplog)[0].error_type == "RuntimeError"


def test_plugin_raising_leaks_no_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install_plugin(tmp_path, monkeypatch, '        raise RuntimeError("detail-xyz-42")')
    with bind_namespace(NAMESPACE):
        result = list_repositories_result(_Store())
    assert "detail-xyz-42" not in json.dumps(result)
    assert "detail-xyz-42" not in caplog.text


@pytest.mark.parametrize(
    "body",
    [
        "        return {'repo': 'git.example.com/team/api'}",
        "        return None",
    ],
    ids=["dict", "none"],
)
def test_plugin_returning_a_non_list_answers_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    _install_plugin(tmp_path, monkeypatch, body)
    with bind_namespace(NAMESPACE):
        result = list_repositories_result(_Store())
    assert result == {
        "ok": False,
        "reason": UNAVAILABLE_REASON,
        "correlation_id": "",
    }


def test_plugin_item_missing_repo_answers_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_plugin(
        tmp_path,
        monkeypatch,
        """\
        return [
            {"repo": "git.example.com/team/api", "level": None, "state": None},
            {"level": None, "state": None},
        ]""",
    )
    with bind_namespace(NAMESPACE):
        result = list_repositories_result(_Store())
    assert result["ok"] is False
    assert result["reason"] == UNAVAILABLE_REASON
    assert "repositories" not in result


@pytest.mark.parametrize(
    "repo",
    [
        "https://git.example.com/team/api",
        "git.example.com/team/api?ref=main",
        "git.example.com/team/api/extra",
        "user:token@git.example.com/team/api",
        "git.example.com/team/my api",
        "git.example.com/team/api\n",
        "",
    ],
    ids=["scheme", "query", "extra-segment", "credentials", "space", "newline", "empty"],
)
def test_repo_with_a_disallowed_shape_fails_the_whole_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: str
) -> None:
    _install_plugin(
        tmp_path,
        monkeypatch,
        f"""\
        return [
            {{"repo": "git.example.com/team/good", "level": None, "state": None}},
            {{"repo": {repo!r}, "level": None, "state": None}},
        ]""",
    )
    with bind_namespace(NAMESPACE):
        result = list_repositories_result(_Store())
    assert result == {
        "ok": False,
        "reason": UNAVAILABLE_REASON,
        "correlation_id": "",
    }


def test_non_string_level_answers_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_plugin(
        tmp_path,
        monkeypatch,
        """\
        return [{"repo": "git.example.com/team/api", "level": 3, "state": None}]""",
    )
    with bind_namespace(NAMESPACE):
        result = list_repositories_result(_Store())
    assert result["ok"] is False


def test_bad_plugin_spec_answers_unavailable_without_importing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(REPO_LISTING_PLUGIN_ENV, "os:system")
    with bind_namespace(NAMESPACE):
        result = list_repositories_result(_Store())
    assert result["ok"] is False
    assert result["reason"] == UNAVAILABLE_REASON
    assert len(_warnings(caplog)) == 1


def test_plugin_outside_the_allowlist_answers_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = _install_plugin(tmp_path, monkeypatch, "        return []")
    monkeypatch.setenv(ALLOWLIST_ENV, "some_other_module")
    with bind_namespace(NAMESPACE):
        result = list_repositories_result(_Store())
    assert result["ok"] is False
    assert name not in sys.modules
