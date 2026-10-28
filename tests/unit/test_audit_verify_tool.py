"""Tests for the ``verify_audit`` MCP tool and the chain verifier it exposes.

The host audit stream and the per-plan streams share one verifier:
``_verify_chain_at(path)`` walks a chain and returns the ``seq`` of the first
record that breaks it — a ``seq`` out of step with its position, a ``prev_hash``
that does not name the previous record's ``hash``, or a ``hash`` that no longer
matches the record's own bytes — or ``None`` when the chain is intact. A missing,
empty, or whitespace-only file is intact: there is nothing to disagree with.

``verify_audit`` is the read-only MCP tool over that verifier. It reports
``{"ok": True, "plans": {<plan>: None | <bad seq>}, "host": None | <bad seq>}``
for every plan that has an audit file (a plan with no audit file is absent, not
``None``) and for the host-scoped chain.

The limit these tests pin rather than hide: truncation of the tail is not
detected. A chain with no externally recorded head cannot prove that the newest
records were not removed, so dropping trailing lines still verifies intact.

Fixtures are built by calling the real append helpers in ``pipeline.audit`` so
every record carries a valid ``hash``/``prev_hash`` pair; tampering is then done
on the bytes of one line.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipeline.server
from pipeline import audit as audit_mod
from pipeline import persistence, remote_tool_policy
from pipeline import server_tools_lifecycle as lifecycle
from pipeline.actor import ANONYMOUS, bind_principal

PLAN_SUFFIX = ".audit.jsonl"
PLAN = "alpha"


@pytest.fixture()
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect both audit streams at a temp plan directory for one test.

    ``pipeline.audit`` resolves ``persistence.PLAN_DIR`` at call time, so this
    one seam moves the per-plan streams and the host stream together.
    """
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    return tmp_path


def _append(plan_name: str, count: int) -> Path:
    """Append *count* records to a plan chain and return its path."""
    for _ in range(count):
        audit_mod.append_audit(plan_name, action="story:merge", outcome="allowed")
    return audit_mod.audit_path(plan_name)


def _append_host(count: int) -> Path:
    """Append *count* records to the host chain and return its path."""
    for _ in range(count):
        audit_mod.append_host_audit(action="config:write", outcome="denied")
    return audit_mod.host_audit_path()


def _rewrite_lines(path: Path, mutate) -> None:
    """Apply *mutate* to the file's lines and write them back."""
    lines = path.read_text().splitlines()
    mutate(lines)
    path.write_text("\n".join(lines) + "\n")


def _set_outcome(lines: list[str], index: int) -> None:
    """Change one record's ``outcome`` while leaving its recorded ``hash``."""
    record = json.loads(lines[index])
    record["outcome"] = "refused" if record["outcome"] == "allowed" else "allowed"
    lines[index] = json.dumps(record, sort_keys=True, separators=(",", ":"))


def _delete_index(index: int):
    def mutate(lines: list[str]) -> None:
        del lines[index]

    return mutate


# --------------------------------------------------------------------------
# Chain verification: intact chains
# --------------------------------------------------------------------------


def test_plan_chain_intact_reports_none(plan_dir: Path) -> None:
    _append(PLAN, 3)

    assert audit_mod.verify_audit_chain(PLAN) is None


def test_host_chain_intact_reports_none(plan_dir: Path) -> None:
    _append_host(2)

    assert audit_mod.verify_host_audit_chain() is None


def test_empty_and_missing_audit_files_are_intact(plan_dir: Path) -> None:
    empty = plan_dir / f"empty{PLAN_SUFFIX}"
    empty.write_text("")
    blank = plan_dir / f"blank{PLAN_SUFFIX}"
    blank.write_text("\n \n")

    assert audit_mod.verify_audit_chain("empty") is None
    assert audit_mod.verify_audit_chain("blank") is None
    assert audit_mod.verify_audit_chain("never-written") is None
    assert audit_mod.verify_host_audit_chain() is None


# --------------------------------------------------------------------------
# Chain verification: a broken middle record reports its own seq
# --------------------------------------------------------------------------


def test_plan_chain_edited_middle_line_reports_its_seq(plan_dir: Path) -> None:
    path = _append(PLAN, 4)
    _rewrite_lines(path, lambda lines: _set_outcome(lines, 2))

    # The reported number is the record's own ``seq`` (0-based, like the stream
    # itself), not its 1-based line number and not the last bad line.
    assert json.loads(path.read_text().splitlines()[2])["seq"] == 2
    assert audit_mod.verify_audit_chain(PLAN) == 2


def test_host_chain_edited_middle_line_reports_its_seq(plan_dir: Path) -> None:
    path = _append_host(3)
    _rewrite_lines(path, lambda lines: _set_outcome(lines, 1))

    assert json.loads(path.read_text().splitlines()[1])["seq"] == 1
    assert audit_mod.verify_host_audit_chain() == 1


def test_plan_chain_deleted_middle_line_is_detected(plan_dir: Path) -> None:
    path = _append(PLAN, 4)
    _rewrite_lines(path, _delete_index(1))

    # Records now carry seq 0, 2, 3: the gap is visible at position 1.
    assert [json.loads(line)["seq"] for line in path.read_text().splitlines()] == [
        0,
        2,
        3,
    ]
    assert audit_mod.verify_audit_chain(PLAN) == 1


# --------------------------------------------------------------------------
# The limit: tail truncation is not detected
# --------------------------------------------------------------------------


def test_plan_chain_tail_truncation_is_not_detected(plan_dir: Path) -> None:
    """Dropping trailing records still verifies None.

    Tail truncation is not detected without a recorded chain head: the newest
    records can be removed and what remains is a shorter, self-consistent chain.
    Only a head hash shipped where the pipeline cannot rewrite it can close this,
    so nothing here should be read as proof the newest records survive.
    """
    path = _append(PLAN, 4)
    _rewrite_lines(path, lambda lines: lines.__delitem__(slice(2, None)))

    assert len(path.read_text().splitlines()) == 2
    assert audit_mod.verify_audit_chain(PLAN) is None


# --------------------------------------------------------------------------
# Read-only: verification never rewrites the chain
# --------------------------------------------------------------------------


def test_verify_does_not_modify_the_chain_file(plan_dir: Path) -> None:
    plan_path = _append(PLAN, 3)
    host_path = _append_host(2)
    _rewrite_lines(plan_path, lambda lines: _set_outcome(lines, 1))
    plan_before = plan_path.read_bytes()
    host_before = host_path.read_bytes()

    audit_mod.verify_audit_chain(PLAN)
    audit_mod.verify_host_audit_chain()

    assert plan_path.read_bytes() == plan_before
    assert host_path.read_bytes() == host_before


# --------------------------------------------------------------------------
# The MCP tool
# --------------------------------------------------------------------------


def test_verify_audit_registered_and_in_remote_read() -> None:
    assert "verify_audit" in [tool.__name__ for tool in lifecycle.TOOLS]
    assert "verify_audit" in pipeline.server.mcp._tool_manager._tools
    assert "verify_audit" in remote_tool_policy.REMOTE_READ


def _dispatch_verify_audit() -> dict[str, object]:
    """Call the registered tool the way ``pipeline.server`` mounted it."""
    return pipeline.server.mcp._tool_manager._tools["verify_audit"].fn()


def test_verify_audit_tool_reports_plan_and_host_results(plan_dir: Path) -> None:
    _append("alpha", 3)
    _rewrite_lines(_append("beta", 3), lambda lines: _set_outcome(lines, 2))
    _rewrite_lines(_append_host(2), lambda lines: _set_outcome(lines, 1))
    (plan_dir / "gamma.plan.json").write_text("{}")

    # A non-operator principal sees the same answer: the tool reads, it does
    # not decide, so no plan role applies to it.
    with bind_principal(ANONYMOUS):
        result = _dispatch_verify_audit()

    assert result == {"ok": True, "plans": {"alpha": None, "beta": 2}, "host": 1}


def test_verify_audit_tool_on_empty_plan_dir(plan_dir: Path) -> None:
    with bind_principal(ANONYMOUS):
        result = _dispatch_verify_audit()

    assert result == {"ok": True, "plans": {}, "host": None}
