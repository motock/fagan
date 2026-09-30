"""Re-export contract for the done-marker split out of
scripts/local_agent_oracle.py (RH-11)."""
import json
from pathlib import Path

from scripts import local_agent_oracle_done_marker as new

from tests.unit._local_agent_oracle_test_helpers import lao

REPO = Path(__file__).parent.parent.parent
LINE_LIMIT = 1000


def _line_count(path: Path) -> int:
    with path.open(encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def test_should_expose_done_reasons_as_same_object():
    assert lao._DONE_REASONS is new._DONE_REASONS


def test_should_import_moved_symbols_from_new_module():
    assert callable(new.write_done_marker_impl)
    assert new._DONE_REASONS[0] == "done"


def test_should_keep_write_done_marker_on_original_module():
    assert callable(lao.write_done_marker)


def test_should_honour_cwd_patched_on_original_module(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)

    lao.write_done_marker(2)

    marker = json.loads((tmp_path / ".agent_done").read_text(encoding="utf-8"))
    assert marker["reason"] == "parked"


def test_should_not_downgrade_existing_done_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(lao, "CWD", tmp_path)
    lao.write_done_marker(0)

    lao.write_done_marker(1)

    marker = json.loads((tmp_path / ".agent_done").read_text(encoding="utf-8"))
    assert marker["exit_code"] == 0


def test_should_map_unknown_rc_to_error(tmp_path):
    new.write_done_marker_impl(tmp_path, 99)

    marker = json.loads((tmp_path / ".agent_done").read_text(encoding="utf-8"))
    assert marker["reason"] == "error"


def test_should_keep_original_module_under_line_limit():
    assert _line_count(REPO / "scripts" / "local_agent_oracle.py") < LINE_LIMIT


def test_should_keep_new_module_under_line_limit():
    assert _line_count(REPO / "scripts" / "local_agent_oracle_done_marker.py") < LINE_LIMIT
