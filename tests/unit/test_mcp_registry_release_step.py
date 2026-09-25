"""RELEASING.md covers publishing to the MCP registry, and server.json's
description matches how the project describes itself (REG-1)."""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASING = REPO_ROOT / "docs" / "RELEASING.md"
SERVER_JSON = REPO_ROOT / "server.json"


def _releasing() -> str:
    return RELEASING.read_text(encoding="utf-8")


def _description() -> str:
    return json.loads(SERVER_JSON.read_text(encoding="utf-8"))["description"]


def test_server_description_says_open_weight_models_write_the_code():
    assert "open-weight models" in _description()


def test_server_description_no_longer_says_a_local_model_implements():
    assert "a local model implements" not in _description()


def test_server_description_fits_the_registry_schema():
    assert 1 <= len(_description()) <= 100


def test_releasing_doc_has_the_publish_command():
    assert "mcp-publisher publish" in _releasing()


def test_releasing_doc_has_the_login_command():
    assert "mcp-publisher login github" in _releasing()


def test_registry_step_comes_after_the_github_release_and_before_after_the_release():
    text = _releasing()
    release = text.index("gh release create")
    publish = text.index("mcp-publisher publish")
    after = text.index("## After the release")
    assert release < publish < after


def test_releasing_doc_says_server_json_version_tracks_the_changelog():
    text = _releasing()
    assert "server.json" in text
    assert "`version` must match" in text
