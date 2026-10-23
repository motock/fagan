import hashlib
import json

import pytest

from pipeline import context_sources as cs
from pipeline import paths


def _source(root, **overrides):
    source = {"name": "docs", "uri": str(root), "include": ["**/*"], "exclude": [], "max_chars": 20000}
    source.update(overrides)
    return source


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "ctx"
    (root / "sub").mkdir(parents=True)
    (root / "b.md").write_text("bee\n")
    (root / "a.md").write_text("ay\n")
    (root / "sub" / "c.txt").write_text("sea\n")
    return root


def test_should_concatenate_sorted_files_with_headers(tree):
    result = cs.resolve_dir_source(_source(tree))
    assert result["text"] == "--- a.md ---\nay\n--- b.md ---\nbee\n--- sub/c.txt ---\nsea\n"
    assert result["files"] == 3
    assert result["bytes"] == len(result["text"].encode())
    assert result["truncated"] is False


def test_should_apply_include_and_exclude(tree):
    result = cs.resolve_dir_source(_source(tree, include=["**/*.md"], exclude=["b.md"]))
    assert result["text"] == "--- a.md ---\nay\n"
    assert result["files"] == 1


def test_should_skip_symlink_escaping_root(tree, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret-ish\n")
    (tree / "link.txt").symlink_to(outside)
    result = cs.resolve_dir_source(_source(tree))
    assert "link.txt" not in result["text"]
    assert "secret-ish" not in result["text"]


def test_should_follow_symlink_inside_root(tree):
    (tree / "alias.txt").symlink_to(tree / "a.md")
    assert "--- alias.txt ---\nay\n" in cs.resolve_dir_source(_source(tree))["text"]


def test_should_skip_binary_file(tree):
    (tree / "bin.dat").write_bytes(b"abc\x00def")
    assert "bin.dat" not in cs.resolve_dir_source(_source(tree))["text"]


def test_should_skip_file_over_one_mib(tree):
    (tree / "big.txt").write_text("x" * (1024 * 1024 + 1))
    assert "big.txt" not in cs.resolve_dir_source(_source(tree))["text"]


def test_should_keep_file_of_exactly_one_mib(tree):
    (tree / "edge.txt").write_text("x" * (1024 * 1024))
    assert "edge.txt" in cs.resolve_dir_source(_source(tree, max_chars=100000))["text"]


def test_should_truncate_with_visible_marker(tree):
    result = cs.resolve_dir_source(_source(tree, max_chars=20))
    assert result["truncated"] is True
    assert result["text"].endswith("[... truncated at 20 chars ...]")
    assert result["text"].startswith("--- a.md ---\nay\n--- \n")


def test_should_not_mark_truncated_when_under_cap(tree):
    assert cs.resolve_dir_source(_source(tree))["truncated"] is False


def test_should_have_stable_digest_over_returned_text(tree):
    first = cs.resolve_dir_source(_source(tree))
    second = cs.resolve_dir_source(_source(tree))
    assert first["digest"] == second["digest"]
    assert first["digest"] == "sha256:" + hashlib.sha256(first["text"].encode()).hexdigest()


def test_should_change_digest_when_content_changes(tree):
    before = cs.resolve_dir_source(_source(tree))["digest"]
    (tree / "a.md").write_text("changed\n")
    assert cs.resolve_dir_source(_source(tree))["digest"] != before


def test_should_raise_for_missing_root(tmp_path):
    with pytest.raises(cs.ContextSourceError, match="not a directory"):
        cs.resolve_dir_source(_source(tmp_path / "nope"))


def test_should_raise_when_root_is_a_file(tree):
    with pytest.raises(cs.ContextSourceError, match="not a directory"):
        cs.resolve_dir_source(_source(tree / "a.md"))


_SECRETS = {
    "PEM private key": "-----BEGIN RSA PRIVATE KEY-----\nabc\n",
    "AWS access key": "AKIA" + "A1B2C3D4E5F6G7H8",
    "GitHub token": "ghp_" + "a" * 36,
    "API key": "sk-" + "b" * 24,
}


@pytest.mark.parametrize("label", list(_SECRETS))
def test_should_label_each_secret_pattern(label):
    assert label in cs.scan_for_secrets("prefix " + _SECRETS[label])


def test_should_return_no_labels_for_clean_text():
    assert cs.scan_for_secrets("just docs, sk-short and AKIA123") == []


@pytest.mark.parametrize("label", list(_SECRETS))
def test_should_reject_secret_file_without_echoing_value(tree, label):
    (tree / "leak.txt").write_text(_SECRETS[label])
    with pytest.raises(cs.ContextSourceError) as exc:
        cs.resolve_dir_source(_source(tree))
    message = str(exc.value)
    assert "leak.txt" in message
    assert label in message
    assert _SECRETS[label].strip().splitlines()[-1] not in message
    assert "a" * 36 not in message and "b" * 24 not in message


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path / "plans")
    return tmp_path / "plans"


def test_should_write_snapshot_content_and_meta(plan_dir):
    digest = cs.snapshot_context("hello")
    hexdigest = hashlib.sha256(b"hello").hexdigest()
    assert digest == "sha256:" + hexdigest
    folder = plan_dir / "context" / hexdigest
    assert (folder / "content.txt").read_text() == "hello"
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["digest"] == digest
    assert meta["bytes"] == 5


def test_should_not_rewrite_existing_snapshot(plan_dir):
    cs.snapshot_context("hello")
    content = plan_dir / "context" / hashlib.sha256(b"hello").hexdigest() / "content.txt"
    content.write_text("sentinel")
    cs.snapshot_context("hello")
    assert content.read_text() == "sentinel"


def test_should_leave_no_temp_files_in_snapshot(plan_dir):
    cs.snapshot_context("hello")
    folder = plan_dir / "context" / hashlib.sha256(b"hello").hexdigest()
    assert sorted(p.name for p in folder.iterdir()) == ["content.txt", "meta.json"]
