"""FileStore manifest writes go through the shared atomic JSON writer.

The on-disk format and the interception seam both matter: every direct writer in
the codebase calls ``parsers._atomic_write_json`` (two-space indent, tmp+replace),
and the test suite patches that seam on ``pipeline.server``. ``FileStore`` used to
spell out its own compact ``json.dumps`` + ``os.replace``; these tests pin the
shared writer instead.
"""

from __future__ import annotations

import json

import pytest

import pipeline.server as p
from pipeline.store import FileStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "PLAN_DIR", tmp_path)
    return FileStore()


def test_should_write_the_manifest_with_two_space_indentation(store, tmp_path):
    manifest = {"plan_name": "demo", "stories": [{"id": "s1"}], "n": 3}

    store.save_manifest("demo", manifest)

    text = (tmp_path / "demo.manifest.json").read_text()
    assert text == json.dumps(manifest, indent=2)


def test_should_route_the_write_through_the_server_atomic_writer(
    store, tmp_path, monkeypatch
):
    calls = []

    def spy(path, obj):
        calls.append((path, obj))

    monkeypatch.setattr(p, "_atomic_write_json", spy)
    manifest = {"plan_name": "demo"}

    store.save_manifest("demo", manifest)

    assert calls == [(tmp_path / "demo.manifest.json", manifest)]


def test_should_leave_the_old_manifest_untouched_when_the_writer_raises(
    store, tmp_path, monkeypatch
):
    store.save_manifest("demo", {"plan_name": "demo", "generation": 1})
    before = (tmp_path / "demo.manifest.json").read_text()

    def boom(path, obj):
        raise OSError("disk full")

    monkeypatch.setattr(p, "_atomic_write_json", boom)

    with pytest.raises(OSError):
        store.save_manifest("demo", {"plan_name": "demo", "generation": 2})

    assert (tmp_path / "demo.manifest.json").read_text() == before


def test_should_leave_no_tmp_file_after_a_non_serialisable_manifest(
    store, tmp_path
):
    with pytest.raises(TypeError):
        store.save_manifest("demo", {"plan_name": "demo", "bad": object()})

    assert not (tmp_path / "demo.manifest.json").exists()
    assert list(tmp_path.glob("*.tmp*")) == []
