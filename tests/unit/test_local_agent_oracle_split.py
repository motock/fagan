"""Re-export contract for the tamper-restore split out of
scripts/local_agent_oracle.py (RH-11)."""
from pathlib import Path

from scripts import local_agent_oracle_done_marker as new
from tests.unit._local_agent_oracle_test_helpers import lao

REPO = Path(__file__).parent.parent.parent
LINE_LIMIT = 1000


def _line_count(path: Path) -> int:
    with path.open(encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def test_should_import_moved_symbol_from_new_module():
    assert callable(new.restore_tampered_oracle_files_impl)


def test_should_keep_wrapper_on_original_module():
    assert callable(lao._restore_tampered_oracle_files)
    assert callable(lao._capture_oracle_snapshot)


def test_should_honour_cwd_patched_on_original_module(tmp_path, monkeypatch):
    # Monkeypatch reach: CWD patched on the original module must steer the moved code.
    (tmp_path / "acc.py").write_text("original")
    monkeypatch.setattr(lao, "CWD", tmp_path)
    monkeypatch.setattr(lao, "ACCEPTANCE_PATHS", ["acc.py"])
    lao._capture_oracle_snapshot()
    (tmp_path / "acc.py").write_text("tampered")

    warning = lao._restore_tampered_oracle_files()

    assert "acc.py" in warning
    assert (tmp_path / "acc.py").read_text() == "original"


def test_should_return_empty_when_untouched(tmp_path):
    (tmp_path / "acc.py").write_text("x")

    assert new.restore_tampered_oracle_files_impl({"acc.py": "x"}, tmp_path) == ""


def test_should_delete_file_that_did_not_exist_at_snapshot(tmp_path):
    (tmp_path / "acc.py").write_text("created later")

    warning = new.restore_tampered_oracle_files_impl({"acc.py": None}, tmp_path)

    assert "acc.py" in warning
    assert not (tmp_path / "acc.py").exists()


def test_should_keep_original_module_under_line_limit():
    assert _line_count(REPO / "scripts" / "local_agent_oracle.py") < LINE_LIMIT


def test_should_keep_new_module_under_line_limit():
    assert _line_count(REPO / "scripts" / "local_agent_oracle_done_marker.py") < LINE_LIMIT
