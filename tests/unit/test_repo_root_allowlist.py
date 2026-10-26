"""ENT-PREP-01: opt-in ``PIPELINE_ALLOWED_REPO_ROOTS`` allowlist for plan repo_root.

With the variable unset (or exactly empty) every code path must behave exactly
as today.  When it is set to a non-empty value it is an allowlist: a candidate
repo root is allowed only when it equals an entry or is a descendant of one.
Every malformed entry fails the whole check closed (deny), and the error names
the variable without echoing the candidate path.

``save_plan`` is the enforced boundary; ``ingest_plan`` is deliberately not
enforced in this story.
"""

import json
import os

import pytest

from pipeline import server as p
from pipeline.workspace import ALLOWED_REPO_ROOTS_ENV, check_repo_root_allowed

ENV = "PIPELINE_ALLOWED_REPO_ROOTS"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """PLAN_DIR redirected into tmp_path (mirrors test_service_save_plan_repo_root)."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    return d


# --- validator: unset / empty means "no restriction" ------------------------


def test_unset_env_allows_any_path(monkeypatch, tmp_path):
    monkeypatch.delenv(ENV, raising=False)
    assert check_repo_root_allowed(str(tmp_path / "anywhere" / "at" / "all")) is None


def test_exactly_empty_env_allows_any_path(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV, "")
    assert check_repo_root_allowed(str(tmp_path / "anywhere")) is None


# --- validator: allow cases -------------------------------------------------


def test_exact_root_allows_root_and_descendant(monkeypatch, tmp_path):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setenv(ENV, str(root))
    assert check_repo_root_allowed(str(root)) is None
    assert check_repo_root_allowed(str(root / "project" / "nested")) is None


def test_multiple_entries_one_matching_allows(monkeypatch, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv(ENV, os.pathsep.join([str(first), str(second)]))
    assert check_repo_root_allowed(str(second / "project")) is None


def test_tilde_prefixed_entry_expands(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "repos").mkdir()
    monkeypatch.setenv(ENV, "~/repos")
    assert check_repo_root_allowed(str(tmp_path / "repos" / "project")) is None


# --- validator: deny cases (fail closed) ------------------------------------


def test_outside_every_entry_denies(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv(ENV, str(allowed))
    with pytest.raises(ValueError):
        check_repo_root_allowed(str(tmp_path / "outside"))


def test_relative_entry_denies(monkeypatch, tmp_path):
    """cwd=<tmp>, env "repos", candidate <tmp>/repos/project -> DENY.

    A careless ``Path("repos").resolve()`` would yield <tmp>/repos and wrongly
    allow the candidate, so the entry must be rejected as relative.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repos").mkdir()
    monkeypatch.setenv(ENV, "repos")
    with pytest.raises(ValueError):
        check_repo_root_allowed(str(tmp_path / "repos" / "project"))


def test_sibling_prefix_is_not_a_descendant(monkeypatch, tmp_path):
    """env <tmp>/repos, candidate <tmp>/repos-evil/project -> DENY.

    A careless ``str(candidate).startswith(str(entry))`` would wrongly allow it.
    """
    (tmp_path / "repos").mkdir()
    monkeypatch.setenv(ENV, str(tmp_path / "repos"))
    with pytest.raises(ValueError):
        check_repo_root_allowed(str(tmp_path / "repos-evil" / "project"))


def test_unresolvable_entry_denies_with_value_error(monkeypatch):
    monkeypatch.setenv(ENV, "~nosuchuser/repo")
    with pytest.raises(ValueError) as excinfo:
        check_repo_root_allowed("/srv/repos/project")
    # Must be a ValueError, never the RuntimeError expanduser() raises.
    assert not isinstance(excinfo.value, RuntimeError)


def test_whitespace_only_value_denies(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV, "   ")
    with pytest.raises(ValueError):
        check_repo_root_allowed(str(tmp_path / "repos"))


def test_empty_entry_in_list_denies(monkeypatch, tmp_path):
    """env ":" -> two empty entries -> DENY (fail closed)."""
    monkeypatch.setenv(ENV, os.pathsep)
    with pytest.raises(ValueError):
        check_repo_root_allowed(str(tmp_path / "repos"))


def test_invalid_entry_denies_even_when_another_entry_matches(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv(ENV, os.pathsep.join([str(allowed), "relative/repo"]))
    with pytest.raises(ValueError):
        check_repo_root_allowed(str(allowed / "project"))


def test_error_names_variable_and_hides_candidate(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    candidate = str(tmp_path / "secret-candidate" / "project")
    monkeypatch.setenv(ENV, str(allowed))
    with pytest.raises(ValueError) as excinfo:
        check_repo_root_allowed(candidate)
    message = str(excinfo.value)
    assert ENV in message
    assert candidate not in message
    assert "secret-candidate" not in message


def test_env_is_read_per_call_not_cached(monkeypatch, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    candidate = str(second / "project")

    monkeypatch.setenv(ENV, str(first))
    with pytest.raises(ValueError):
        check_repo_root_allowed(candidate)

    monkeypatch.setenv(ENV, str(second))
    assert check_repo_root_allowed(candidate) is None


# --- save_plan: the enforced boundary ---------------------------------------


def test_save_plan_denies_outside_root_and_leaves_file_untouched(
    plan_dir, monkeypatch, tmp_path
):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv(ENV, str(allowed))

    target = plan_dir / "t12.json"
    before = b'{"repo_root": "/previous", "epics": []}'
    target.write_bytes(before)

    svc = p.PipelineService()
    result = svc.save_plan("t12", json.dumps({"repo_root": "/outside", "epics": []}))

    assert result["ok"] is False
    assert ENV in result["error"]
    assert target.read_bytes() == before


def test_save_plan_allows_permitted_root_and_writes(plan_dir, monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv(ENV, str(allowed))

    svc = p.PipelineService()
    result = svc.save_plan(
        "t13", json.dumps({"repo_root": str(allowed / "project"), "epics": []})
    )

    assert result["ok"] is True
    written = json.loads((plan_dir / "t13.json").read_text())
    assert written["repo_root"] == str(allowed / "project")


def test_save_plan_denial_then_allow_proves_no_partial_write(
    plan_dir, monkeypatch, tmp_path
):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    target = plan_dir / "t15.json"
    before = b'{"repo_root": "/previous", "epics": []}'
    target.write_bytes(before)

    svc = p.PipelineService()
    plan = json.dumps({"repo_root": str(outside), "epics": []})

    monkeypatch.setenv(ENV, str(allowed))
    denied = svc.save_plan("t15", plan)
    assert denied["ok"] is False
    assert target.read_bytes() == before

    monkeypatch.setenv(ENV, str(outside))
    allowed_result = svc.save_plan("t15", plan)
    assert allowed_result["ok"] is True
    assert json.loads(target.read_text())["repo_root"] == str(outside)


def test_save_plan_with_workspace_does_not_consult_allowlist(
    plan_dir, monkeypatch, tmp_path
):
    """A supplied workspace keeps the existing validate_workspace overwrite path."""
    calls = []

    def exploding(path):
        calls.append(path)
        raise AssertionError("allowlist must not run when workspace is supplied")

    monkeypatch.setattr("pipeline.service.check_repo_root_allowed", exploding, raising=False)
    monkeypatch.setattr(
        "pipeline.service.validate_workspace",
        lambda workspace: {"ok": True, "path": str(tmp_path / "ws"), "error": None},
        raising=False,
    )
    monkeypatch.setenv(ENV, str(tmp_path / "allowed"))

    svc = p.PipelineService()
    result = svc.save_plan(
        "t14", json.dumps({"repo_root": "/outside", "epics": []}), workspace="/tmp/ws"
    )

    assert result["ok"] is True
    assert calls == []
    written = json.loads((plan_dir / "t14.json").read_text())
    assert written["repo_root"] == str(tmp_path / "ws")


def test_allowed_repo_roots_env_constant():
    assert ALLOWED_REPO_ROOTS_ENV == "PIPELINE_ALLOWED_REPO_ROOTS"
