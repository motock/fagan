"""The bench harness must not commit the .venv symlink it creates.

`setup_workspace` symlinks `<repo>/.venv` to the live pipeline venv so a
dispatched agent resolves an interpreter (`_venv_python_for`), then writes a
fixture `.gitignore` for the repo. That pattern was `.venv/` - gitignore's
trailing-slash form matches *directories only* - so the symlink was ignored
by neither `.gitignore` nor `.git/info/exclude`, and every pytest-ecosystem
bench repo carried a machine-specific absolute symlink in its init commit.
Verified live 2026-09-27: `cron_field__qwen36__t0`'s repo had
`.venv -> /Users/jessecarroll/git/fagan/.venv` tracked.

This drives the real writer rather than reading the pattern back out of the
source, so it also fails if the entry is corrected somewhere other than the
line `setup_workspace` actually writes.
"""
import subprocess

from tests.benchmark import harness


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )


def test_init_commit_does_not_track_the_venv_symlink(tmp_path):
    """End-to-end through setup_workspace: the fixture symlink must be
    ignored by the .gitignore that setup_workspace itself writes."""
    repo = harness.setup_workspace(tmp_path / "cell")["repo"]

    assert (repo / ".venv").is_symlink(), "setup_workspace's own fixture changed"
    tracked = _git("ls-files", cwd=repo).stdout.split()

    assert ".venv" not in tracked, (
        "setup_workspace committed its own .venv symlink - the .gitignore "
        "pattern must not use gitignore's directory-only 'dir/' form, which "
        "does not match a symlink"
    )
    assert "pyproject.toml" in tracked, "real scaffold files must stay tracked"
