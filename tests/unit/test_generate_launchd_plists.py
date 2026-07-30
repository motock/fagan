"""Tests for scripts/generate_launchd_plists.sh and the launchd/*.template
files it consumes (maturity plan A4 - "Externalize per-machine assumptions").

These are narrower, implementation-facing tests that complement the
read-only acceptance fixture in test_acceptance_launchd_plist_portability.py
(which is the authoritative spec and must not be edited). This file adds
coverage the acceptance fixture does not: per-key substitution checks for
every generated plist kind, CLI-default behavior (--out-dir, --repo-root,
--mlx-model-path all optional), the "only real per-machine values get
templated" rule (advance-scheduler's deliberate REPO_ROOT placeholder must
stay literal), script style/self-location conventions, and stricter negative
cases around the fail-closed --mlx-model-path requirement.

On current master this file fails at collection/first-test: neither
scripts/generate_launchd_plists.sh nor the launchd/*.plist.template files
exist yet. That is the expected starting state.
"""
import os
import plistlib
import re
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent.parent
_LAUNCHD = _REPO / "launchd"
_GENERATOR = _REPO / "scripts" / "generate_launchd_plists.sh"
_INSTALL_SH = _REPO / "scripts" / "install.sh"

_KINDS = ("advance-scheduler", "usage-poller", "mlx-supervisor")

_REAL_HOME_PATH_FRAGMENT = "/Users/jessecarroll"

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


def _run_generator(args, env=None, check=True):
    return subprocess.run(
        [str(_GENERATOR), *args],
        capture_output=True, text=True, check=check, env=env,
    )


@pytest.fixture
def fake_repo_and_out(tmp_path):
    fake_repo = tmp_path / "fake-repo"
    fake_repo.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    return fake_repo, out_dir


@pytest.fixture
def fake_home_env(tmp_path):
    fake_home = tmp_path / "fake-home"
    env = {**os.environ, "HOME": str(fake_home)}
    return fake_home, env


# ---------- script existence / style ----------

def test_generator_is_executable_shell_script():
    assert _GENERATOR.is_file(), f"missing {_GENERATOR}"
    mode = _GENERATOR.stat().st_mode
    assert mode & stat.S_IXUSR, "generator must be chmod +x"


def test_generator_uses_strict_bash_mode_like_install_sh():
    text = _GENERATOR.read_text()
    assert "set -euo pipefail" in text, (
        "generator should follow install.sh's strict-mode convention"
    )


def test_generator_auto_detects_its_own_repo_root_like_install_sh():
    text = _GENERATOR.read_text()
    assert 'dirname "${BASH_SOURCE[0]}"' in text, (
        "generator must auto-detect its own location the same way "
        "scripts/install.sh does, so it can find launchd/*.template "
        "regardless of the caller's cwd"
    )


def test_install_sh_was_not_modified_by_this_change():
    # This task must not touch scripts/install.sh at all.
    text = _INSTALL_SH.read_text()
    assert "REQ=\"requirements.txt\"" in text
    assert "generate_launchd_plists" not in text


# ---------- templates: structural fidelity ----------

def test_all_three_templates_exist():
    for kind in _KINDS:
        path = _LAUNCHD / f"com.claude.pipeline.{kind}.plist.template"
        assert path.is_file(), f"missing template {path}"


def test_no_template_contains_real_machine_home_path():
    for kind in _KINDS:
        path = _LAUNCHD / f"com.claude.pipeline.{kind}.plist.template"
        text = path.read_text()
        assert _REAL_HOME_PATH_FRAGMENT not in text, (
            f"{path.name} still hardcodes {_REAL_HOME_PATH_FRAGMENT}"
        )


def test_advance_scheduler_and_mlx_supervisor_templates_use_tabs():
    for kind in ("advance-scheduler", "mlx-supervisor"):
        path = _LAUNCHD / f"com.claude.pipeline.{kind}.plist.template"
        text = path.read_text()
        assert "\n\t<key>" in text, (
            f"{path.name} must preserve the committed file's tab indentation"
        )


def test_usage_poller_template_uses_four_space_indentation():
    path = _LAUNCHD / "com.claude.pipeline.usage-poller.plist.template"
    text = path.read_text()
    assert "\n    <key>" in text, (
        f"{path.name} must preserve the committed file's 4-space indentation"
    )


def test_mlx_supervisor_template_preserves_explanatory_comment_verbatim():
    path = _LAUNCHD / "com.claude.pipeline.mlx-supervisor.plist.template"
    text = path.read_text()
    assert "uv venv --python 3.14 .venv-mlx" in text, (
        "the explanatory XML comment (including its literal `--python`) "
        "must be preserved verbatim in the template"
    )
    assert "borrowed an unrelated project's venv" in text


def test_advance_scheduler_template_keeps_placeholder_repo_root_env_literal():
    # REPO_ROOT in EnvironmentVariables is a deliberate placeholder value in
    # the committed plist, NOT a per-machine path — it must NOT be
    # parameterized to {{REPO_ROOT}}.
    path = _LAUNCHD / "com.claude.pipeline.advance-scheduler.plist.template"
    text = path.read_text()
    assert "/nonexistent-repo-root-set-per-plan-only" in text, (
        "advance-scheduler template must keep the deliberate REPO_ROOT "
        "placeholder literal, not substitute it with {{REPO_ROOT}}"
    )


def test_templates_use_only_the_three_documented_placeholder_tokens():
    import re
    token_pattern = re.compile(r"\{\{([A-Z_]+)\}\}")
    allowed = {"REPO_ROOT", "HOME", "MLX_MODEL_PATH"}
    for kind in _KINDS:
        path = _LAUNCHD / f"com.claude.pipeline.{kind}.plist.template"
        found = set(token_pattern.findall(path.read_text()))
        assert found <= allowed, (
            f"{path.name} uses undocumented placeholder token(s): {found - allowed}"
        )


def test_only_mlx_supervisor_template_uses_mlx_model_path_token():
    for kind in ("advance-scheduler", "usage-poller"):
        path = _LAUNCHD / f"com.claude.pipeline.{kind}.plist.template"
        assert "{{MLX_MODEL_PATH}}" not in path.read_text(), (
            f"{path.name} must not reference {{{{MLX_MODEL_PATH}}}}"
        )
    mlx_path = _LAUNCHD / "com.claude.pipeline.mlx-supervisor.plist.template"
    assert "{{MLX_MODEL_PATH}}" in mlx_path.read_text()


# ---------- generator output: file names ----------

def test_generator_writes_exactly_the_three_expected_output_filenames(
    fake_repo_and_out, fake_home_env,
):
    fake_repo, out_dir = fake_repo_and_out
    _, env = fake_home_env
    _run_generator(
        ["--repo-root", str(fake_repo), "--out-dir", str(out_dir),
         "--mlx-model-path", "/fake/model/cache"],
        env=env,
    )
    produced = {p.name for p in out_dir.glob("*.plist")}
    assert produced == {
        "com.claude.pipeline.advance-scheduler.plist",
        "com.claude.pipeline.usage-poller.plist",
        "com.claude.pipeline.mlx-supervisor.plist",
    }
    # The .template suffix must be dropped, not carried through.
    assert not list(out_dir.glob("*.template"))


# ---------- generator output: per-key substitution, all three plists ----------

def test_mlx_supervisor_full_substitution(fake_repo_and_out, fake_home_env):
    fake_repo, out_dir = fake_repo_and_out
    fake_home, env = fake_home_env
    _run_generator(
        ["--repo-root", str(fake_repo), "--out-dir", str(out_dir),
         "--mlx-model-path", "/fake/model/cache"],
        env=env,
    )
    mlx = _plutil_load(out_dir / "com.claude.pipeline.mlx-supervisor.plist")
    assert mlx["WorkingDirectory"] == str(fake_repo)
    assert mlx["ProgramArguments"] == [
        str(fake_repo / ".venv-mlx" / "bin" / "python3"),
        str(fake_repo / "scripts" / "mlx_server_supervisor.py"),
    ]
    assert mlx["StandardOutPath"] == str(fake_repo / "mlx-supervisor.log")
    assert mlx["StandardErrorPath"] == str(fake_repo / "mlx-supervisor.err.log")
    assert mlx["EnvironmentVariables"]["MLX_SERVER_PYTHON"] == str(
        fake_repo / ".venv-mlx" / "bin" / "python3"
    )
    assert mlx["EnvironmentVariables"]["MLX_SERVER_MODEL_PATH"] == "/fake/model/cache"
    assert str(fake_home) in mlx["EnvironmentVariables"]["PATH"]
    assert _REAL_HOME_PATH_FRAGMENT not in mlx["EnvironmentVariables"]["PATH"]
    # Fields untouched by any placeholder must survive unchanged.
    assert mlx["Label"] == "com.claude.pipeline.mlx-supervisor"
    assert mlx["EnvironmentVariables"]["MLX_SERVER_PORT"] == "8080"
    assert mlx["StartInterval"] == 120
    assert mlx["AbandonProcessGroup"] is True


def test_usage_poller_full_substitution(fake_repo_and_out, fake_home_env):
    fake_repo, out_dir = fake_repo_and_out
    fake_home, env = fake_home_env
    _run_generator(
        ["--repo-root", str(fake_repo), "--out-dir", str(out_dir),
         "--mlx-model-path", "/fake/model/cache"],
        env=env,
    )
    poller = _plutil_load(out_dir / "com.claude.pipeline.usage-poller.plist")
    assert poller["WorkingDirectory"] == str(fake_repo)
    assert poller["ProgramArguments"][0] == str(fake_repo / ".venv" / "bin" / "python3")
    assert poller["StandardOutPath"] == str(fake_repo / "usage-poller.log")
    assert poller["StandardErrorPath"] == str(fake_repo / "usage-poller.err.log")
    assert str(fake_home) in poller["EnvironmentVariables"]["PATH"]
    assert _REAL_HOME_PATH_FRAGMENT not in poller["EnvironmentVariables"]["PATH"]
    assert poller["Label"] == "com.claude.pipeline.usage-poller"
    assert poller["StartInterval"] == 60


def test_advance_scheduler_full_substitution(fake_repo_and_out, fake_home_env):
    fake_repo, out_dir = fake_repo_and_out
    fake_home, env = fake_home_env
    _run_generator(
        ["--repo-root", str(fake_repo), "--out-dir", str(out_dir),
         "--mlx-model-path", "/fake/model/cache"],
        env=env,
    )
    scheduler = _plutil_load(out_dir / "com.claude.pipeline.advance-scheduler.plist")
    assert scheduler["WorkingDirectory"] == str(fake_repo)
    assert scheduler["ProgramArguments"][0] == str(fake_repo / ".venv" / "bin" / "python3")
    assert scheduler["StandardOutPath"] == str(fake_repo / "advance-scheduler.log")
    assert scheduler["StandardErrorPath"] == str(fake_repo / "advance-scheduler.err.log")
    env_vars = scheduler["EnvironmentVariables"]
    assert str(fake_home) in env_vars["PATH"]
    assert _REAL_HOME_PATH_FRAGMENT not in env_vars["PATH"]
    # The deliberate placeholder must NOT be replaced with the fake repo
    # root — it is not a per-machine path token.
    assert env_vars["REPO_ROOT"] == "/nonexistent-repo-root-set-per-plan-only"
    # Untouched knobs must survive unchanged.
    assert env_vars["PIPELINE_LOCAL_MAX_STEPS"] == "60"
    assert scheduler["Label"] == "com.claude.pipeline.advance-scheduler"


# ---------- CLI defaults ----------

def test_out_dir_defaults_to_repo_root_slash_launchd(fake_home_env):
    """--out-dir is optional; when omitted, output must land in
    <repo-root>/launchd. Uses a throwaway fake --repo-root so nothing near
    the real, committed launchd/*.plist files is touched."""
    import tempfile
    _, env = fake_home_env
    with tempfile.TemporaryDirectory() as tmp:
        fake_repo = Path(tmp) / "fake-repo-default-out"
        fake_repo.mkdir()
        _run_generator(
            ["--repo-root", str(fake_repo), "--mlx-model-path", "/fake/model/cache"],
            env=env,
        )
        default_out = fake_repo / "launchd"
        assert (default_out / "com.claude.pipeline.advance-scheduler.plist").is_file()
        assert (default_out / "com.claude.pipeline.usage-poller.plist").is_file()
        assert (default_out / "com.claude.pipeline.mlx-supervisor.plist").is_file()


def test_repo_root_defaults_to_generators_own_repo_root(tmp_path, fake_home_env):
    """--repo-root is optional; when omitted, it must default to the
    generator script's own auto-detected repo root (this repo), NOT the
    caller's cwd. --out-dir is redirected to tmp_path so the real,
    committed launchd/*.plist files are never touched or overwritten."""
    _, env = fake_home_env
    out_dir = tmp_path / "default-repo-root-out"
    out_dir.mkdir()
    _run_generator(
        ["--out-dir", str(out_dir), "--mlx-model-path", "/fake/model/cache"],
        env=env,
    )
    poller = _plutil_load(out_dir / "com.claude.pipeline.usage-poller.plist")
    assert poller["WorkingDirectory"] == str(_REPO)


def test_mlx_model_path_defaults_to_env_var_when_flag_omitted(fake_repo_and_out, fake_home_env):
    fake_repo, out_dir = fake_repo_and_out
    _, base_env = fake_home_env
    env = {**base_env, "MLX_MODEL_PATH": "/env/supplied/model/cache"}
    _run_generator(
        ["--repo-root", str(fake_repo), "--out-dir", str(out_dir)],
        env=env,
    )
    mlx = _plutil_load(out_dir / "com.claude.pipeline.mlx-supervisor.plist")
    assert mlx["EnvironmentVariables"]["MLX_SERVER_MODEL_PATH"] == "/env/supplied/model/cache"


def test_explicit_mlx_model_path_flag_overrides_env_var(fake_repo_and_out, fake_home_env):
    fake_repo, out_dir = fake_repo_and_out
    _, base_env = fake_home_env
    env = {**base_env, "MLX_MODEL_PATH": "/env/supplied/model/cache"}
    _run_generator(
        ["--repo-root", str(fake_repo), "--out-dir", str(out_dir),
         "--mlx-model-path", "/flag/supplied/model/cache"],
        env=env,
    )
    mlx = _plutil_load(out_dir / "com.claude.pipeline.mlx-supervisor.plist")
    assert mlx["EnvironmentVariables"]["MLX_SERVER_MODEL_PATH"] == "/flag/supplied/model/cache"


# ---------- fail-closed negative cases ----------

def test_missing_mlx_model_path_and_unset_env_fails_before_writing_any_file(
    fake_repo_and_out, fake_home_env,
):
    fake_repo, out_dir = fake_repo_and_out
    _, base_env = fake_home_env
    env = dict(base_env)
    env.pop("MLX_MODEL_PATH", None)

    result = _run_generator(
        ["--repo-root", str(fake_repo), "--out-dir", str(out_dir)],
        env=env, check=False,
    )
    assert result.returncode != 0
    assert not list(out_dir.glob("*.plist")), (
        "no output file may be written when --mlx-model-path is missing "
        "and MLX_MODEL_PATH is unset"
    )
    assert not list(out_dir.glob("*")), (
        "no partial/dropped file of any kind may be left behind on failure"
    )


def test_missing_mlx_model_path_error_message_is_actionable(
    fake_repo_and_out, fake_home_env,
):
    fake_repo, out_dir = fake_repo_and_out
    _, base_env = fake_home_env
    env = dict(base_env)
    env.pop("MLX_MODEL_PATH", None)

    result = _run_generator(
        ["--repo-root", str(fake_repo), "--out-dir", str(out_dir)],
        env=env, check=False,
    )
    combined_output = (result.stdout + result.stderr).lower()
    assert "mlx" in combined_output and "model" in combined_output, (
        "error message must clearly mention the missing mlx model path "
        "requirement, not fail silently or with an unrelated message"
    )


def test_empty_string_mlx_model_path_flag_is_rejected(fake_repo_and_out, fake_home_env):
    """Boundary case: an explicitly empty string is not a real path either
    — it must be treated the same as "not provided", not accepted as a
    valid (if useless) value."""
    fake_repo, out_dir = fake_repo_and_out
    _, base_env = fake_home_env
    env = dict(base_env)
    env.pop("MLX_MODEL_PATH", None)

    result = _run_generator(
        ["--repo-root", str(fake_repo), "--out-dir", str(out_dir),
         "--mlx-model-path", ""],
        env=env, check=False,
    )
    assert result.returncode != 0
    assert not list(out_dir.glob("*.plist"))


def test_nonexistent_repo_root_does_not_produce_a_silently_wrong_plist(
    tmp_path, fake_home_env,
):
    """Boundary case: --repo-root pointing at a path that doesn't exist on
    disk. The generator does not need to validate this (the plist is valid
    even if the path is created later), but it must still substitute the
    literal value given rather than silently falling back to something
    else — this guards against a default-swallowing bug in arg parsing."""
    _, env = fake_home_env
    nonexistent_repo = tmp_path / "does-not-exist-yet"
    out_dir = tmp_path / "out-nonexistent-case"
    out_dir.mkdir()
    _run_generator(
        ["--repo-root", str(nonexistent_repo), "--out-dir", str(out_dir),
         "--mlx-model-path", "/fake/model/cache"],
        env=env,
    )
    poller = _plutil_load(out_dir / "com.claude.pipeline.usage-poller.plist")
    assert poller["WorkingDirectory"] == str(nonexistent_repo)
