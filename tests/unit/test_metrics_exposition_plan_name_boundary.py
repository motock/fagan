"""``fagan_plans`` still validates every plan name it is asked to read.

``_plan_samples`` used to run :func:`pipeline.parsers._validate_key` over each
name before handing it to a manifest read, because a plan name flows into a
filesystem path (``<plans_root>/<plan_name>.manifest.json``). Delegating the
listing to :func:`pipeline.manifest_source.read_plan_names` moved the names'
origin from a local glob to the record store's ``list_plans``, whose names are
not separator-checked there (note the defensive ``str(name)`` cast), so the
guard has to stay in the scrape path:

* a store-supplied ``"../escape"`` must never be read, so an out-of-tree
  manifest cannot be counted;
* a ``...manifest.json`` file in the plan directory yields the dot-only name
  ``".."``, which the dashboard's plan routes reject, so ``fagan_plans`` must
  not count it either.
"""

from __future__ import annotations

import json

import pytest

from pipeline import metrics_exposition as m
from pipeline import server
from pipeline.store_records import DEFAULT_NAMESPACE


class _Records:
    """A record view answering the two reads the plan count needs."""

    def __init__(self, names=(), manifests=None) -> None:
        self._names = list(names)
        self._manifests = manifests or {}
        self.requested: list[tuple[str, str]] = []

    def list_plans(self, namespace: str) -> list[str]:
        assert namespace == DEFAULT_NAMESPACE
        return list(self._names)

    def get_manifest(self, namespace: str, plan_name: str):
        self.requested.append((namespace, plan_name))
        return self._manifests.get(plan_name)


class _Store:
    def __init__(self, records) -> None:
        self._records = records

    def records(self):
        return self._records


def _approved(status: str) -> dict:
    return {"approval": {"status": status}}


def _counts(snapshot, status: str) -> int:
    return {labels["status"]: value for labels, value in snapshot["fagan_plans"]}[status]


@pytest.fixture(autouse=True)
def quiet_produce(monkeypatch):
    """Keep the other producers quiet: this file only cares about the plans."""
    monkeypatch.setattr(m, "health_extras", lambda plan_dir=None: {})
    monkeypatch.setattr(m, "outbox_summary", lambda plan_dir=None: {})
    monkeypatch.setattr(m, "consumer_status", dict)
    monkeypatch.setattr(m, "service_level_percentiles", lambda plan_dir=None: {})


def test_a_store_supplied_traversal_name_is_never_read(monkeypatch, tmp_path) -> None:
    (tmp_path.parent / "escape.manifest.json").write_text(
        json.dumps(_approved("approved")), encoding="utf-8"
    )
    records = _Records(
        names=["../escape", "alpha"], manifests={"alpha": _approved("rejected")}
    )
    monkeypatch.setattr(server, "_store", _Store(records))

    snapshot = m.collect(plan_dir=tmp_path)

    assert _counts(snapshot, "approved") == 0
    assert _counts(snapshot, "rejected") == 1
    assert (DEFAULT_NAMESPACE, "../escape") not in records.requested


def test_a_dot_only_name_is_not_counted(monkeypatch, tmp_path) -> None:
    (tmp_path / "...manifest.json").write_text(
        json.dumps(_approved("approved")), encoding="utf-8"
    )
    monkeypatch.setattr(server, "_store", _Store(_Records(names=[])))

    snapshot = m.collect(plan_dir=tmp_path)

    assert _counts(snapshot, "approved") == 0
    assert _counts(snapshot, "none") == 0


def test_an_ordinary_name_still_counts(monkeypatch, tmp_path) -> None:
    (tmp_path / "v1.2.3.manifest.json").write_text(
        json.dumps(_approved("approved")), encoding="utf-8"
    )
    monkeypatch.setattr(server, "_store", _Store(_Records(names=[])))

    snapshot = m.collect(plan_dir=tmp_path)

    assert _counts(snapshot, "approved") == 1
