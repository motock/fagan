import subprocess
from pathlib import Path

from pipeline.git_ops import _commit_wip


def init_repo(tmp: Path):
    # initialize git repo and make initial commit with a file
    subprocess.run(["git", "init"], cwd=tmp, check=True)
    (tmp / "foo.txt").write_text("original\n")
    subprocess.run(["git", "add", "foo.txt"], cwd=tmp, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp, check=True)


def test_guard_restores_deleted_file(tmp_path: Path):
    init_repo(tmp_path)
    # delete file to simulate kill-mid-write
    (tmp_path / "foo.txt").unlink()
    sha = _commit_wip(str(tmp_path), "S1", "step", guard_against_deletion=True)
    # after commit, file should exist with original content
    assert (tmp_path / "foo.txt").read_text() == "original\n"
    # verify commit contains the file
    out = subprocess.check_output(["git", "show", f"{sha}:foo.txt"], cwd=tmp_path).decode()
    assert out.strip() == "original"


def test_no_guard_commits_deletion(tmp_path: Path):
    init_repo(tmp_path)
    (tmp_path / "foo.txt").unlink()
    sha = _commit_wip(str(tmp_path), "S1", "step")  # default guard=False
    # file should be absent after commit
    assert not (tmp_path / "foo.txt").exists()
    # verify commit shows deletion: git show HEAD:foo.txt should error
    try:
        subprocess.check_output(["git", "show", f"{sha}:foo.txt"], cwd=tmp_path)
        found = True
    except subprocess.CalledProcessError:
        found = False
    assert not found


def test_guard_allows_modification(tmp_path: Path):
    init_repo(tmp_path)
    # modify file content
    (tmp_path / "foo.txt").write_text("modified\n")
    sha = _commit_wip(str(tmp_path), "S1", "step", guard_against_deletion=True)
    assert (tmp_path / "foo.txt").read_text() == "modified\n"
    out = subprocess.check_output(["git", "show", f"{sha}:foo.txt"], cwd=tmp_path).decode()
    assert out.strip() == "modified"
