"""c3-3: the dashboard's UI preferences are read and written only via HostState.

``app/dashboard.py`` used to carry a private ``_dashboard_ui_state_path()``
that built ``PLAN_DIR / ".dashboard_ui_state.json"`` plus a ``PLAN_DIR``
LiveRef binding that existed only for it. The archived-plan helpers already
went through ``_host_state``; these tests grade that the dashboard no longer
builds the file path itself and that the round-trip still holds through the
host state, including the corrupt-file fallback.
"""

import inspect
import json

import pipeline.server as pipeline_server
from app import dashboard


def test_dashboard_source_does_not_build_the_ui_state_file_path():
    assert ".dashboard_ui_state.json" not in inspect.getsource(dashboard)


def test_dashboard_no_longer_defines_a_ui_state_path_helper():
    assert not hasattr(dashboard, "_dashboard_ui_state_path")


def test_dashboard_no_longer_binds_plan_dir():
    assert not hasattr(dashboard, "PLAN_DIR")


def test_archived_plans_round_trip_through_the_host_state(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline_server, "PLAN_DIR", tmp_path)

    dashboard._write_archived_plans({"beta", "alpha"})

    assert dashboard._read_archived_plans() == {"alpha", "beta"}
    assert json.loads((tmp_path / ".dashboard_ui_state.json").read_text()) == {
        "archived": ["alpha", "beta"]
    }


def test_corrupt_ui_state_file_reads_as_nothing_archived(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline_server, "PLAN_DIR", tmp_path)
    (tmp_path / ".dashboard_ui_state.json").write_text("{not json")

    assert dashboard._read_archived_plans() == set()


def test_wrong_shaped_ui_state_file_reads_as_nothing_archived(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline_server, "PLAN_DIR", tmp_path)
    (tmp_path / ".dashboard_ui_state.json").write_text('["not", "a", "dict"]')

    assert dashboard._read_archived_plans() == set()
