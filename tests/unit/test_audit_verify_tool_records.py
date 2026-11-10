"""``verify_audit`` reads its chains through the ``AuditLog``, not through a glob.

The tool used to enumerate ``<PLAN_DIR>/*.audit.jsonl`` itself and verify each
file it found.  It now asks an :class:`pipeline.audit_log.AuditLog` which
chains exist and reads each chain back through that log, so the answer comes
from the same surface the writers append to.

What that buys, and what is pinned here:

* a plan is reported when the log knows the chain -- the chain the log lists,
  not a file name that merely looks like one;
* a stream parked inside the plan directory that the log does not know as a
  plan chain (an unaddressable name, or the host stream under another name) is
  not reported as a plan;
* the host chain is reported under ``host``, never under ``plans``;
* the anchor block is present only when ``PIPELINE_AUDIT_ANCHOR`` names a sink,
  and an unreadable anchor log is reported as a single ``*`` entry that says the
  anchor log is unreadable -- never as a clean verdict for any chain.

The result shape is unchanged: ``{"ok": True, "plans": {<plan>: None | <seq>},
"host": None | <seq>}``, with ``"anchor"`` added only when anchoring is on.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import pipeline.server
from pipeline import audit as audit_mod
from pipeline import persistence
from pipeline.actor import ANONYMOUS, bind_principal
from pipeline.audit_file import FileAuditLog
from pipeline.store_records import DEFAULT_NAMESPACE

PLAN_SUFFIX = ".audit.jsonl"
ANCHOR_VARIABLE = "PIPELINE_AUDIT_ANCHOR"


@pytest.fixture()
def plan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect both audit streams at a temp plan directory for one test.

    ``pipeline.audit`` resolves ``persistence.PLAN_DIR`` at call time, so this
    one seam moves the per-plan streams and the host stream together.  The
    directory lives under ``tmp_path`` but is not ``tmp_path`` itself, so an
    anchor log can be parked outside the plan directory (the anchor sink
    refuses a path inside it).
    """
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(persistence, "PLAN_DIR", directory)
    monkeypatch.delenv(ANCHOR_VARIABLE, raising=False)
    return directory


@pytest.fixture()
def anchor_file(tmp_path: Path, plan_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Enable anchoring to an append-only log outside the plan directory."""
    directory = tmp_path / "anchor"
    directory.mkdir()
    path = directory / "heads.jsonl"
    monkeypatch.setenv(ANCHOR_VARIABLE, f"file:{path}")
    return path


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


def _audit_chain_at(plan_name: str) -> list[dict | None]:
    """Return the entries stored for *plan_name*, as the log reads them back."""
    return FileAuditLog().read_chain(DEFAULT_NAMESPACE, plan_name)


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


@contextmanager
def _refuse_reads(monkeypatch: pytest.MonkeyPatch, path: Path) -> Iterator[None]:
    """Make opening *path* raise for the duration of the ``with`` block.

    ``chmod`` cannot be used: root ignores the mode bits and an append-only
    mount refuses the change, so the refusal is injected instead.
    """
    real_open = Path.open

    def _refuse(self: Path, *args: object, **kwargs: object):
        if self == path:
            raise PermissionError(13, "permission denied")
        return real_open(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", _refuse)
        yield


def _dispatch_verify_audit() -> dict[str, object]:
    """Call the registered tool the way ``pipeline.server`` mounted it."""
    return pipeline.server.mcp._tool_manager._tools["verify_audit"].fn()


# --------------------------------------------------------------------------
# A tampered plan chain is reported with the seq of its first broken record
# --------------------------------------------------------------------------


def test_tampered_plan_chain_reports_its_first_bad_seq(plan_dir: Path) -> None:
    path = _append("alpha", 4)
    _rewrite_lines(path, lambda lines: _set_outcome(lines, 1))

    with bind_principal(ANONYMOUS):
        result = _dispatch_verify_audit()

    assert result["plans"] == {"alpha": 1}


def test_intact_plan_chain_reports_none(plan_dir: Path) -> None:
    _append("alpha", 3)

    with bind_principal(ANONYMOUS):
        result = _dispatch_verify_audit()

    assert result["plans"] == {"alpha": None}


def test_several_plans_are_reported_together(plan_dir: Path) -> None:
    _append("alpha", 2)
    _rewrite_lines(_append("beta", 3), lambda lines: _set_outcome(lines, 2))

    with bind_principal(ANONYMOUS):
        result = _dispatch_verify_audit()

    assert result["plans"] == {"alpha": None, "beta": 2}


# --------------------------------------------------------------------------
# The host chain is reported under "host", never under "plans"
# --------------------------------------------------------------------------


def test_host_chain_is_reported_under_host_not_plans(plan_dir: Path) -> None:
    _append("alpha", 2)
    _rewrite_lines(_append_host(3), lambda lines: _set_outcome(lines, 1))

    with bind_principal(ANONYMOUS):
        result = _dispatch_verify_audit()

    assert result["host"] == 1
    assert "@host" not in result["plans"]
    assert set(result["plans"]) == {"alpha"}


def test_intact_host_chain_reports_none(plan_dir: Path) -> None:
    _append_host(2)

    with bind_principal(ANONYMOUS):
        result = _dispatch_verify_audit()

    assert result["host"] is None
    assert result["plans"] == {}


# --------------------------------------------------------------------------
# A stream parked in the plan directory that is not a plan chain is not
# reported as a plan
# --------------------------------------------------------------------------


def test_redirected_host_stream_in_plan_dir_is_not_reported_as_a_plan(
    plan_dir: Path,
) -> None:
    """A host stream linked into the plan directory is not a plan chain.

    The host stream belongs to no plan.  When it is reachable from the plan
    directory under a name that looks like a plan stream, it must not be
    reported as one: the chain the log knows is the host chain, and that one is
    reported under ``host``.
    """
    _append("alpha", 2)
    host = _append_host(2)
    (plan_dir / f"ghost{PLAN_SUFFIX}").symlink_to(host)

    with bind_principal(ANONYMOUS):
        result = _dispatch_verify_audit()

    assert "ghost" not in result["plans"]
    assert set(result["plans"]) == {"alpha"}


def test_a_stream_whose_name_is_not_an_addressable_plan_is_not_reported(
    plan_dir: Path,
) -> None:
    """A stream the log cannot address is skipped, not reported as a plan.

    The name is not a legal plan name, so the log does not know it as a chain.
    It must be left out of the answer rather than blow the read up.
    """
    _append("alpha", 2)
    (plan_dir / f"not a plan{PLAN_SUFFIX}").write_text("{}\n")

    with bind_principal(ANONYMOUS):
        result = _dispatch_verify_audit()

    assert set(result["plans"]) == {"alpha"}


# --------------------------------------------------------------------------
# The chains come from the log, not from a directory listing
# --------------------------------------------------------------------------


def test_the_plan_chains_are_the_ones_the_log_lists(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answer follows the log's chain list, not the files on disk.

    A stream on disk that the log does not list is not a chain, so it is not
    reported -- and a chain the log lists is reported even when nothing has
    been written to it yet.
    """
    _append("alpha", 2)
    _append("beta", 2)

    def _only_alpha(self, namespace: str) -> list[str]:
        return ["alpha"]

    monkeypatch.setattr(FileAuditLog, "list_chains", _only_alpha)

    with bind_principal(ANONYMOUS):
        result = _dispatch_verify_audit()

    assert result["plans"] == {"alpha": None}


def test_the_chains_are_verified_through_the_log(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verdict comes from the entries the log reads back.

    The file on disk is intact, but the log hands back a rewritten entry, so
    the tool must report the break rather than trusting the file.
    """
    _append("alpha", 3)
    entries = _audit_chain_at("alpha")
    broken = [dict(entry) for entry in entries]
    broken[1]["outcome"] = "refused"

    def _broken(self, namespace: str, chain: str) -> list[dict | None]:
        return broken if chain == "alpha" else []

    monkeypatch.setattr(FileAuditLog, "read_chain", _broken)

    with bind_principal(ANONYMOUS):
        result = _dispatch_verify_audit()

    assert result["plans"] == {"alpha": 1}


# --------------------------------------------------------------------------
# Boundary: an empty plan directory
# --------------------------------------------------------------------------


def test_empty_plan_directory_reports_no_plans_and_no_host(plan_dir: Path) -> None:
    with bind_principal(ANONYMOUS):
        result = _dispatch_verify_audit()

    assert result == {"ok": True, "plans": {}, "host": None}


def test_result_shape_is_unchanged(plan_dir: Path) -> None:
    """The answer keeps its three keys and their types."""
    _append("alpha", 1)

    with bind_principal(ANONYMOUS):
        result = _dispatch_verify_audit()

    assert result["ok"] is True
    assert isinstance(result["plans"], dict)
    assert result["host"] is None or isinstance(result["host"], int)
    assert "anchor" not in result


# --------------------------------------------------------------------------
# The anchor block
# --------------------------------------------------------------------------


def test_no_anchor_block_when_the_anchor_is_not_configured(plan_dir: Path) -> None:
    with bind_principal(ANONYMOUS):
        result = _dispatch_verify_audit()

    assert "anchor" not in result


def test_no_anchor_block_when_the_anchor_is_blank(
    plan_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ANCHOR_VARIABLE, "   ")

    with bind_principal(ANONYMOUS):
        result = _dispatch_verify_audit()

    assert "anchor" not in result


def test_anchor_block_is_present_when_the_anchor_is_configured(
    plan_dir: Path, anchor_file: Path
) -> None:
    _append("alpha", 2)

    with bind_principal(ANONYMOUS):
        result = _dispatch_verify_audit()

    assert "anchor" in result
    assert isinstance(result["anchor"], dict)


def test_unreadable_anchor_log_reports_a_single_star_entry(
    plan_dir: Path, anchor_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An anchor log that cannot be read is reported, never swallowed.

    The verdict names the failure, so that no chain is reported clean while
    the anchors are unknown.
    """
    anchor_file.write_text("{}\n")

    with _refuse_reads(monkeypatch, anchor_file), bind_principal(ANONYMOUS):
        result = _dispatch_verify_audit()

    assert result["anchor"] == {"*": "anchor log unreadable: PermissionError"}


def test_unreadable_anchor_log_reports_no_clean_chain(
    plan_dir: Path, anchor_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable anchor log means "unknown", so nothing is reported clean."""
    anchor_file.write_text("{}\n")

    with _refuse_reads(monkeypatch, anchor_file), bind_principal(ANONYMOUS):
        result = _dispatch_verify_audit()

    assert set(result["anchor"]) == {"*"}
