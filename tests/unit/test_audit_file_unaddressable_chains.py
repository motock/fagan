"""``FileAuditLog.unaddressable_chains``: the streams ``list_chains`` skips, named.

``list_chains`` drops a ``*.audit.jsonl`` file whose name is not an addressable
plan name, and returns ``[]`` when the directory cannot be listed.  Its contract
for other callers is unchanged; this sibling method exists so the importer can
refuse each skipped stream instead of exiting 0 over it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pipeline import audit_chain
from pipeline.audit_file import SUFFIX, FileAuditLog
from pipeline.audit_log import HOST_CHAIN_ID
from pipeline.store_records import DEFAULT_NAMESPACE, UnknownNamespaceError

NS = DEFAULT_NAMESPACE


def _chain_bytes() -> str:
    record = audit_chain.link(None, {"action": "plan.approve", "outcome": "allowed"})
    return audit_chain.canonical(record) + "\n"


def _plans(tmp_path: Path, *names: str) -> Path:
    plans = tmp_path / "plans"
    plans.mkdir()
    for name in names:
        (plans / f"{name}{SUFFIX}").write_text(_chain_bytes())
    return plans


def test_an_unaddressable_stream_name_is_reported_and_list_chains_still_skips_it(tmp_path):
    plans = _plans(tmp_path, "alpha", "bad name")

    log = FileAuditLog(plans)

    assert log.unaddressable_chains(NS) == ["bad name"]
    assert log.list_chains(NS) == ["alpha"]


def test_a_colon_in_the_stream_name_is_reported_as_unaddressable(tmp_path):
    plans = _plans(tmp_path, "bad:name")

    assert FileAuditLog(plans).unaddressable_chains(NS) == ["bad:name"]


def test_a_directory_of_addressable_streams_reports_nothing(tmp_path):
    plans = _plans(tmp_path, "alpha", "beta")

    assert FileAuditLog(plans).unaddressable_chains(NS) == []


def test_the_host_stream_is_never_reported_as_unaddressable(tmp_path):
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / ".audit.host.jsonl").write_text(_chain_bytes())

    log = FileAuditLog(plans)

    assert log.unaddressable_chains(NS) == []
    assert log.list_chains(NS) == [HOST_CHAIN_ID]


def test_a_missing_plans_directory_reports_nothing(tmp_path):
    assert FileAuditLog(tmp_path / "absent").unaddressable_chains(NS) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-0 directory")
def test_an_unlistable_plans_directory_raises_rather_than_reporting_zero(tmp_path):
    plans = _plans(tmp_path, "bad name")
    plans.chmod(0o000)
    try:
        with pytest.raises(OSError):
            FileAuditLog(plans).unaddressable_chains(NS)
    finally:
        plans.chmod(0o700)


def test_a_non_default_namespace_is_refused(tmp_path):
    with pytest.raises(UnknownNamespaceError):
        FileAuditLog(_plans(tmp_path)).unaddressable_chains("other")
