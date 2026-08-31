"""Acceptance (RETRO-01): both merge paths feed the retro PENDING backlog."""
import importlib


def test_retro_pending_wired_into_merge_and_advance(tmp_path, monkeypatch):
    server = importlib.import_module("pipeline.server")
    merge = importlib.import_module("pipeline.merge")
    advance = importlib.import_module("pipeline.advance")
    pending = tmp_path / "PENDING.md"
    monkeypatch.setattr(server, "RETRO_PENDING_PATH", pending, raising=False)

    manifest = {
        "repo_root": str(server.PIPELINE_SELF_REPO_ROOT),
        "stories": {"a": {"status": "done"}},
    }
    merge._maybe_record_retro("acceptance-retro-fix", manifest)
    advance._maybe_record_retro("acceptance-retro-fix", manifest)

    lines = pending.read_text().splitlines() if pending.exists() else []
    hits = [ln for ln in lines if ln.startswith("- acceptance-retro-fix ")]
    assert len(hits) == 1
