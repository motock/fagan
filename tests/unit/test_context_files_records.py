"""files-variant snapshots: canonical record, HostState cache, guarded clone root."""

import hashlib
import json
from pathlib import Path

import pytest

from pipeline import context_sources as cs
from pipeline import paths, persistence, server
from pipeline.host_state import build_host_state
from pipeline.store_records import DEFAULT_NAMESPACE, InvalidRecordKeyError

FILES = {"a.txt": "alpha\n", "sub/dir/b.md": "beta\n"}
TEXT = "--- a.txt ---\nalpha\n--- sub/dir/b.md ---\nbeta\n"
SOURCE = {"name": "docs", "provider": "dir", "uri": "/some/where", "mode": "files"}


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", d)
    monkeypatch.setattr(persistence, "PLAN_DIR", d)
    monkeypatch.setattr(paths, "PLAN_DIR", d)
    return d


def _canonical(mapping):
    return json.dumps(mapping, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _no_git(*_args, **_kwargs):
    raise AssertionError("git must not run for a refused sha")


def test_should_store_two_file_snapshot_as_canonical_json_record(plan_dir):
    content = cs.snapshot_content(SOURCE, TEXT)

    digest = cs.snapshot_context(content)

    assert content == _canonical(FILES)
    assert digest == "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    stored = server._store.records().get_context_snapshot(DEFAULT_NAMESPACE, digest)
    assert stored["content"] == content


def test_should_materialize_both_files_into_host_cache(plan_dir):
    content = cs.snapshot_content(SOURCE, TEXT)
    digest = cs.snapshot_context(content)

    folder = Path(cs.materialize_context_files(SOURCE, content))

    assert folder == build_host_state().context_cache_dir(digest)
    assert (folder / "files" / "a.txt").read_text() == "alpha\n"
    assert (folder / "files" / "sub" / "dir" / "b.md").read_text() == "beta\n"


@pytest.mark.parametrize("key", ["../x", "/etc/x", "a/../../x"])
def test_should_refuse_unsafe_key_with_nothing_written(plan_dir, key):
    content = _canonical({"ok.txt": "fine", key: "bad"})

    with pytest.raises(cs.ContextSourceError, match="unsafe"):
        cs.materialize_context_files(SOURCE, content)

    assert not (plan_dir / "context-cache").exists()


def test_should_still_load_legacy_text_snapshot(plan_dir):
    digest = cs.snapshot_context(TEXT)

    stored = server._store.records().get_context_snapshot(DEFAULT_NAMESPACE, digest)
    folder = Path(cs.materialize_context_files(SOURCE, stored["content"]))

    assert (folder / "files" / "a.txt").read_text() == "alpha\n"
    assert (folder / "files" / "sub" / "dir" / "b.md").read_text() == "beta\n"


def test_should_return_same_folder_when_materialized_twice(plan_dir):
    content = cs.snapshot_content(SOURCE, TEXT)

    first = cs.materialize_context_files(SOURCE, content)
    second = cs.materialize_context_files(SOURCE, content)

    assert first == second


def test_should_place_clone_root_under_host_root(plan_dir):
    host = build_host_state()

    clone_root = Path(host.git_clone_root()).resolve()

    assert clone_root.is_relative_to(host.root().resolve())
    assert clone_root == (plan_dir / "context" / "git").resolve()


@pytest.mark.parametrize("sha", ["../" + "a" * 40, "a/" + "b" * 39, "a" * 20 + ".." + "a" * 18])
def test_should_refuse_non_full_sha_before_any_git_call(plan_dir, monkeypatch, sha):
    monkeypatch.setattr(cs.subprocess, "run", _no_git)

    with pytest.raises(cs.ContextSourceError, match="clone root"):
        cs._ensure_clone("docs", "https://example.invalid/r.git", sha)

    assert not (plan_dir / "context" / "git").exists()


def test_should_refuse_clone_path_that_resolves_outside_root(plan_dir, tmp_path, monkeypatch):
    sha = "c" * 40
    base = plan_dir / "context" / "git"
    base.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / sha).symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(cs.subprocess, "run", _no_git)

    with pytest.raises(cs.ContextSourceError, match="clone root"):
        cs._ensure_clone("docs", "https://example.invalid/r.git", sha)


def test_should_refuse_cache_dir_for_malformed_digest(plan_dir):
    with pytest.raises(InvalidRecordKeyError):
        build_host_state().context_cache_dir("sha256:../x")
