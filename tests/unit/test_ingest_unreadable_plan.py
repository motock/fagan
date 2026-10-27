"""INGEST-01: an unreadable plan or manifest is a refusal, not a traceback.

``_ingest_plan_impl`` used to read the plan file with a bare
``json.loads(path.read_text())``, so a truncated manifest -- a disk-full write,
an interrupted atomic replace, a hand edit -- aborted the ingest with a
``JSONDecodeError`` instead of the ``{"ok": False, ...}`` contract every other
refusal in the module follows. The later OWN-03 manifest read had the same
defect. Both now go through ``persistence.read_plan_file``.

Fixture shape (PLAN_DIR bindings) mirrors tests/unit/test_plan_ownership.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.ingest (see
# tests/unit/test_ingest_keyless_reingest.py for the import-cycle note).
import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers

_PREFLIGHT = "Preflight: test fixture - not a real plan\n"
_TRUNCATED = '{"epics": {TRUNCATED'


class _RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def create_epic(self, summary: str):
        self.calls.append(("create_epic", summary))

    def create_story(self, summary, description, epic_id, agent):
        self.calls.append(("create_story", summary))


@pytest.fixture
def provider(monkeypatch):
    prov = _RecordingProvider()
    monkeypatch.setattr(p, "get_ticket_provider", lambda: prov)
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: None)
    # Pin the dispatch provider to "claude" so the non-Claude preflight gate
    # never fires (it would consult the operator's real role registry).
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    return prov


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point every PLAN_DIR binding at ``tmp_path``/plans.

    pipeline_persistence and pipeline_concurrency import PLAN_DIR from
    pipeline_paths at module load and read it as a free var, so patches must
    land on their own bindings too; ingest resolves it through
    pipeline.server.
    """
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


def _repo_root(tmp_path: Path) -> Path:
    """A README-only repo_root: no ecosystem marker, so ingest accepts it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n")
    return repo


def _plan(repo_root: Path) -> dict:
    return {
        "name": "p1",
        "repo_root": str(repo_root),
        "epics": [
            {
                "summary": "E1",
                "stories": [
                    {
                        "key": "issue-1",
                        "summary": "S1",
                        "agent_instructions": _PREFLIGHT + "Do the thing.",
                    }
                ],
            }
        ],
    }


def test_truncated_plan_is_a_refusal_not_a_traceback(provider, plan_dir, tmp_path):
    (plan_dir / "p1.json").write_text(_TRUNCATED)

    result = ingest_mod._ingest_plan_impl("p1")

    assert result["ok"] is False and "p1" in result["error"]


def test_truncated_plan_with_overwrite_is_a_refusal_not_a_traceback(
    provider, plan_dir, tmp_path
):
    (plan_dir / "p1.json").write_text(_TRUNCATED)

    result = ingest_mod._ingest_plan_impl("p1", overwrite=True)

    assert result["ok"] is False and "p1" in result["error"]


def test_missing_plan_refusal_is_unchanged(provider, plan_dir):
    result = ingest_mod._ingest_plan_impl("nope")

    assert result == {"ok": False, "error": "No plan named nope"}


def test_plan_that_is_not_a_json_object_is_a_refusal(provider, plan_dir):
    (plan_dir / "p1.json").write_text("[]")

    result = ingest_mod._ingest_plan_impl("p1")

    assert result["ok"] is False and "not a JSON object" in result["error"]


def test_valid_object_without_epics_still_reaches_the_existing_refusal(
    provider, plan_dir
):
    """The read guard passes valid JSON objects through untouched.

    ``_ingest_plan_impl`` has no ``epics`` guard of its own -- that refusal
    ("Plan must contain 'epics' key") lives in ``PipelineService.save_plan``,
    which refuses to *write* such a plan at all. On the ingest path the
    pre-existing structured refusal a ``{}`` plan reaches is the repo_root
    check, so assert that one: the guard must not swallow the path.
    """
    (plan_dir / "p1.json").write_text("{}")

    result = ingest_mod._ingest_plan_impl("p1")

    assert result == {
        "ok": False,
        "error": "Plan repo_root is missing or not a directory: None",
    }


def test_read_plan_file_refuses_an_unreadable_path(tmp_path):
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory")

    _, refusal = ppers.read_plan_file(blocker / "p.json")

    assert refusal is not None and "unreadable" in refusal["error"]


def test_readable_plan_still_ingests(provider, plan_dir, tmp_path):
    (plan_dir / "p1.json").write_text(json.dumps(_plan(_repo_root(tmp_path))))

    result = ingest_mod._ingest_plan_impl("p1")

    assert result["ok"] is True
