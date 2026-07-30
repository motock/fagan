"""Acceptance fixture: launchd plists must be generated from portable
templates instead of existing only as hand-edited files with a personal path
baked in (maturity plan A4 - "Externalize per-machine assumptions"). A plist
is static XML with no shell/env expansion, so this repo cannot just source
env vars into it - a generation step is required.

Grades four things so a half-done change (template exists but the generator
doesn't actually use it correctly, or vice versa) can't pass:
  1. scripts/generate_launchd_plists.sh exists and is executable.
  2. The three committed template files contain no hardcoded personal path.
  3. Running the generator against a FAKE --repo-root/--out-dir reproduces
     the right substitution for each plist kind.
  4. Running the generator against the REAL repo root (output redirected to
     a tmp dir via --out-dir, never touching the committed launchd/*.plist
     files) reproduces their parsed content exactly - proves the templates
     are faithful to what is actually deployed today, not a divergent
     parallel format nobody uses.

On current master this file FAILS at collection/first-test: neither the
generator script nor the template files exist yet.

NOTE: plists are parsed with `plutil` (via _plutil_load), NOT Python's
plistlib. The committed launchd plists carry documentation in XML comments,
some of which contain `--` (e.g. "uv venv --python"); `--` is illegal inside
XML comments, so expat-backed plistlib rejects those files, while Apple's
plutil accepts them. Using plutil keeps the oracle passable against the real
files. Test 4 derives the real repo root from the committed advance-scheduler
plist's WorkingDirectory (rather than this file's own directory, which is a
worktree during grading) so the regenerated-vs-committed equality holds in a
worktree context.
"""
import os
import plistlib
import re
import stat
import subprocess
from pathlib import Path

_REPO = Path(__file__).parent
_LAUNCHD = _REPO / "launchd"
_GENERATOR = _REPO / "scripts" / "generate_launchd_plists.sh"

_KINDS = ("advance-scheduler", "usage-poller", "mlx-supervisor")

_XML_COMMENT_RE = re.compile(rb"<!--.*?-->", re.DOTALL)


def _plutil_load(path):
    """Parse a plist with plistlib, stripping XML comments first.

    plistlib (expat-backed) rejects `--` inside XML comments - and the
    committed mlx-supervisor.plist's header comment contains "uv venv
    --python". Stripping comments before parsing sidesteps that without
    depending on the macOS-only `plutil` binary (unavailable on Linux CI).
    """
    raw = Path(path).read_bytes()
    return plistlib.loads(_XML_COMMENT_RE.sub(b"", raw))


def test_generator_script_exists_and_is_executable():
    assert _GENERATOR.is_file(), f"missing {_GENERATOR}"
    assert _GENERATOR.stat().st_mode & stat.S_IXUSR, (
        "generate_launchd_plists.sh must be executable (chmod +x)"
    )


def test_templates_exist_and_have_no_hardcoded_personal_path():
    for kind in _KINDS:
        path = _LAUNCHD / f"com.claude.pipeline.{kind}.plist.template"
        assert path.is_file(), f"missing template {path}"
        assert "/Users/jessecarroll" not in path.read_text(), (
            f"{path.name} still hardcodes a personal path"
        )


def test_generator_substitutes_fake_repo_root_and_mlx_path(tmp_path, monkeypatch):
    fake_repo = tmp_path / "fake-repo"
    fake_repo.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # {{HOME}} is substituted from $HOME at generation time, so drive it with a
    # fake HOME - otherwise this machine's real HOME (/Users/jessecarroll)
    # lands in PATH and the "no personal path" assertion is unpassable here.
    fake_home = tmp_path / "fake-home"
    env = {**os.environ, "HOME": str(fake_home)}

    subprocess.run(
        [str(_GENERATOR), "--repo-root", str(fake_repo), "--out-dir", str(out_dir),
         "--mlx-model-path", "/fake/model/cache"],
        check=True, env=env,
    )

    scheduler = _plutil_load(out_dir / "com.claude.pipeline.advance-scheduler.plist")
    assert scheduler["WorkingDirectory"] == str(fake_repo)
    assert scheduler["ProgramArguments"][0] == str(fake_repo / ".venv" / "bin" / "python3")
    assert str(fake_repo) in scheduler["StandardOutPath"]
    assert str(fake_home) in scheduler["EnvironmentVariables"]["PATH"]
    assert "/Users/jessecarroll" not in scheduler["EnvironmentVariables"]["PATH"]

    mlx = _plutil_load(out_dir / "com.claude.pipeline.mlx-supervisor.plist")
    assert mlx["EnvironmentVariables"]["MLX_SERVER_MODEL_PATH"] == "/fake/model/cache"
    assert mlx["ProgramArguments"][0] == str(fake_repo / ".venv-mlx" / "bin" / "python3")

    poller = _plutil_load(out_dir / "com.claude.pipeline.usage-poller.plist")
    assert poller["WorkingDirectory"] == str(fake_repo)


def test_generator_errors_clearly_when_mlx_model_path_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("MLX_MODEL_PATH", raising=False)
    fake_repo = tmp_path / "fake-repo2"
    fake_repo.mkdir()
    out_dir = tmp_path / "out2"
    out_dir.mkdir()

    result = subprocess.run(
        [str(_GENERATOR), "--repo-root", str(fake_repo), "--out-dir", str(out_dir)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0, (
        "generator must fail closed when --mlx-model-path is not provided and "
        "MLX_MODEL_PATH is unset, not silently emit a fabricated-looking path"
    )
    assert not list(out_dir.glob("*.plist")), (
        "generator must not write any file before validating required inputs"
    )


def test_regenerating_against_the_real_repo_reproduces_committed_plists(tmp_path):
    """The real committed launchd/*.plist files must parse to exactly what
    the generator produces for THIS repo's actual root - proves the
    templates are faithful, not a divergent parallel format.

    The repo root is derived from the committed advance-scheduler plist's
    WorkingDirectory (the real deployed path), not this file's own
    directory: during grading this file lives in a worktree whose path
    differs from the committed plists' baked-in root, so regenerating
    against the worktree path would never match."""
    advance = _plutil_load(_LAUNCHD / "com.claude.pipeline.advance-scheduler.plist")
    real_repo = Path(advance["WorkingDirectory"])
    current_mlx_path = _plutil_load(
        _LAUNCHD / "com.claude.pipeline.mlx-supervisor.plist"
    )["EnvironmentVariables"]["MLX_SERVER_MODEL_PATH"]

    out_dir = tmp_path / "regen"
    out_dir.mkdir()
    subprocess.run(
        [str(_GENERATOR), "--repo-root", str(real_repo), "--out-dir", str(out_dir),
         "--mlx-model-path", current_mlx_path],
        check=True,
    )

    for kind in _KINDS:
        name = f"com.claude.pipeline.{kind}.plist"
        regenerated = _plutil_load(out_dir / name)
        committed = _plutil_load(_LAUNCHD / name)
        assert regenerated == committed, (
            f"regenerating {name} for this repo's real root does not match "
            f"the committed file's parsed content - template/generator "
            f"diverges from what is actually deployed"
        )