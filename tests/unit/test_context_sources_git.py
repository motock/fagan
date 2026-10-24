import json
import subprocess

import pytest

from pipeline import context_sources as cs
from pipeline import paths


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "lake"
    (root / "sub").mkdir(parents=True)
    (root / "a.md").write_text("ay\n")
    (root / "b.md").write_text("bee\n")
    (root / "sub" / "c.txt").write_text("sea\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture(autouse=True)
def plan_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path / "plans")


def _head(repo):
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _source(uri, **overrides):
    source = {"name": "lake", "provider": "git", "uri": uri, "include": ["**/*"], "exclude": [], "max_chars": 20000}
    source.update(overrides)
    return source


def test_should_resolve_ref_and_record_sha(repo):
    result = cs.resolve_git_source(_source(f"git+file://{repo}#main"))
    assert result["sha"] == _head(repo)
    assert result["text"] == "--- a.md ---\nay\n--- b.md ---\nbee\n--- sub/c.txt ---\nsea\n"
    assert ".git/" not in result["text"]


def test_should_resolve_tag_ref(repo):
    _git(repo, "tag", "v1")
    assert cs.resolve_git_source(_source(f"git+file://{repo}#v1"))["sha"] == _head(repo)


def test_should_apply_nested_include_and_exclude(repo):
    result = cs.resolve_git_source(_source(f"git+file://{repo}#main", include=["sub/**"], exclude=["**/x"]))
    assert result["text"] == "--- sub/c.txt ---\nsea\n"


def test_should_pin_to_sha_after_ref_moves(repo):
    first = cs.resolve_git_source(_source(f"git+file://{repo}#main"))["sha"]
    (repo / "a.md").write_text("changed\n")
    _git(repo, "commit", "-qam", "second")
    second = cs.resolve_git_source(_source(f"git+file://{repo}#main"))
    assert second["sha"] != first
    assert "changed" in second["text"]


def test_should_reuse_existing_clone(repo, monkeypatch):
    uri = f"git+file://{repo}#main"
    sha = cs.resolve_git_source(_source(uri))["sha"]
    marker = paths.PLAN_DIR / "context" / "git" / sha / "marker"
    marker.write_text("kept")
    cs.resolve_git_source(_source(uri))
    assert marker.read_text() == "kept"


def test_should_fail_on_unreachable_remote(tmp_path):
    with pytest.raises(cs.ContextSourceError, match="unreachable|resolve"):
        cs.resolve_git_source(_source(f"git+file://{tmp_path}/nope#main"))


def test_should_fail_on_missing_ref(repo):
    with pytest.raises(cs.ContextSourceError, match="ref"):
        cs.resolve_git_source(_source(f"git+file://{repo}#no-such-branch"))


@pytest.mark.parametrize("ref", ["../escape", "a/../b", "-flag", ""])
def test_should_reject_unsafe_ref(repo, ref):
    with pytest.raises(cs.ContextSourceError, match="ref"):
        cs.resolve_git_source(_source(f"git+file://{repo}#{ref}"))


def test_should_reject_uri_with_credentials():
    with pytest.raises(cs.ContextSourceError, match="credentials") as info:
        cs.resolve_git_source(_source("git+https://user:hunter2@example.com/o/c.git#main"))
    assert "hunter2" not in str(info.value)


@pytest.mark.parametrize("uri", ["https://example.com/o/c.git#main", "git+ssh://example.com/o/c.git#main"])
def test_should_reject_unsupported_scheme(uri):
    with pytest.raises(cs.ContextSourceError, match="scheme"):
        cs.resolve_git_source(_source(uri))


def test_should_not_modify_context_repo(repo):
    before = _head(repo)
    cs.resolve_git_source(_source(f"git+file://{repo}#main"))
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=False)
    assert _head(repo) == before and status.stdout == ""


def test_should_record_sha_in_snapshot_meta(repo):
    result = cs.resolve_git_source(_source(f"git+file://{repo}#main"))
    digest = cs.snapshot_context(result["text"], meta={"sha": result["sha"]})
    meta = json.loads((paths.PLAN_DIR / "context" / digest.split(":")[1] / "meta.json").read_text())
    assert meta["sha"] == result["sha"]
