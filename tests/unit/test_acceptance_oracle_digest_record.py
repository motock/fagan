"""Acceptance oracle: dispatch must record a digest of each acceptance fixture's
AUTHORITATIVE manifest source, so later gates can prove the worktree copy was
not rewritten.

The digest must come from the manifest source, never from the file on disk -
digesting the file would happily bless a fixture an agent had already edited.
"""
import hashlib
import inspect

import pipeline.server as srv
from pipeline.oracle_gate import acceptance_digests


def test_digest_is_sha256_of_the_manifest_source():
    source = "def test_x():\n    assert True\n"
    story = {"acceptance": [{"path": "tests/unit/test_x.py", "source": source}]}
    assert acceptance_digests(story) == {
        "tests/unit/test_x.py": hashlib.sha256(source.encode()).hexdigest()
    }


def test_multiple_fixtures_each_get_a_digest():
    story = {
        "acceptance": [
            {"path": "a.py", "source": "a"},
            {"path": "b.py", "source": "b"},
        ]
    }
    assert sorted(acceptance_digests(story)) == ["a.py", "b.py"]


def test_story_without_acceptance_yields_no_digests():
    assert acceptance_digests({"summary": "s"}) == {}


def test_dispatch_story_records_the_digests_on_the_story():
    src = inspect.getsource(srv._dispatch_story_impl)
    assert "acceptance_digests" in src, (
        "dispatch_story must record the digests; nothing downstream can detect "
        "tampering without them"
    )
    materialize = src.index('target.write_text(entry["source"])')
    assert src.index("acceptance_digests") > materialize
