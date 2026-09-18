"""Tests for scripts/generate_systemd_units.sh and the systemd/*.template
files it consumes (Linux counterpart of scripts/generate_launchd_plists.sh).

This file is deliberately independent of tests/unit/test_generate_launchd_plists.py
(it reuses the same *patterns* - temp repo dir, temp HOME, running the
generator as a subprocess - but shares no fixtures or helpers with it).

On the branch this file lands on, scripts/generate_systemd_units.sh does not
exist yet, so every behavioral test here fails at the "generator exists"
fixture with a clear message. That is the expected starting state: the
implementation is written against these tests in a later dispatch.
"""
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent.parent
_SYSTEMD = _REPO / "systemd"
_GENERATOR = _REPO / "scripts" / "generate_systemd_units.sh"
_LAUNCHD_GENERATOR = _REPO / "scripts" / "generate_launchd_plists.sh"

# The exact, complete set of outputs the generator must produce. Asserted as a
# set (not just membership) so an accidental extra output file is caught.
_EXPECTED_OUTPUTS = {
    "com.fagan.pipeline.advance-scheduler.service",
    "com.fagan.pipeline.usage-poller.service",
    "com.fagan.pipeline.usage-poller.timer",
    "pipeline-logs.logrotate.conf",
}

_SERVICE_OUTPUTS = (
    "com.fagan.pipeline.advance-scheduler.service",
    "com.fagan.pipeline.usage-poller.service",
)

# The .timer and .logrotate templates carry no {{HOME}} token.
_NON_SERVICE_OUTPUTS = (
    "com.fagan.pipeline.usage-poller.timer",
    "pipeline-logs.logrotate.conf",
)

_TEMPLATES = (
    "com.fagan.pipeline.advance-scheduler.service.template",
    "com.fagan.pipeline.usage-poller.service.template",
    "com.fagan.pipeline.usage-poller.timer.template",
    "pipeline-logs.logrotate.template",
)

# Output filename -> the template it is rendered from (the logrotate output is
# renamed, so it cannot be derived by appending ".template").
_TEMPLATE_FOR_OUTPUT = {
    "com.fagan.pipeline.advance-scheduler.service":
        "com.fagan.pipeline.advance-scheduler.service.template",
    "com.fagan.pipeline.usage-poller.service":
        "com.fagan.pipeline.usage-poller.service.template",
    "com.fagan.pipeline.usage-poller.timer":
        "com.fagan.pipeline.usage-poller.timer.template",
    "pipeline-logs.logrotate.conf": "pipeline-logs.logrotate.template",
}

# The exact SCRIPT_DIR resolution line the launchd generator uses; the systemd
# generator must resolve its own repo root the same way.
_SCRIPT_DIR_LINE = 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"'

_TOKEN_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


def _run_generator(args, env=None, check=True):
    return subprocess.run(
        [str(_GENERATOR), *args],
        capture_output=True, text=True, check=check, env=env,
    )


def _generator_text():
    """Read the generator, failing with a clear message (not a bare
    FileNotFoundError) while the implementation is still missing."""
    assert _GENERATOR.is_file(), f"missing implementation: {_GENERATOR}"
    return _GENERATOR.read_text()


@pytest.fixture
def generator():
    """Fail with a clear message (not a bare FileNotFoundError) if the
    implementation is missing - this is the expected RED state."""
    assert _GENERATOR.is_file(), f"missing implementation: {_GENERATOR}"
    return _GENERATOR


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
    assert mode & stat.S_IXUSR, "generator must be chmod +x (executable from a fresh checkout)"


def test_generator_uses_strict_bash_mode():
    text = _generator_text()
    assert "set -euo pipefail" in text, (
        "generator must use strict bash mode, matching scripts/generate_launchd_plists.sh"
    )


def test_generator_self_locates_repo_root_like_launchd_generator():
    text = _generator_text()
    assert _SCRIPT_DIR_LINE in text, (
        "generator must resolve SCRIPT_DIR exactly the way "
        "scripts/generate_launchd_plists.sh does"
    )
    assert 'REPO_ROOT="$SCRIPT_DIR"' in text


def test_generator_template_dir_is_script_dir_systemd():
    text = _generator_text()
    assert 'TEMPLATE_DIR="${SCRIPT_DIR}/systemd"' in text


def test_generator_chmods_itself_at_runtime():
    text = _generator_text()
    assert 'chmod +x "$0"' in text, (
        "generator must chmod itself at runtime, matching the launchd generator's last line"
    )


def test_generator_has_no_mlx_references():
    text = _generator_text()
    assert "MLX_MODEL_PATH" not in text, (
        "the systemd generator must not reference MLX_MODEL_PATH at all"
    )
    assert "--mlx-model-path" not in text, (
        "the systemd generator must not accept a --mlx-model-path flag"
    )
    assert "mlx" not in text.lower(), (
        "the systemd generator must contain no MLX-related logic (there is no mlx-supervisor unit)"
    )


def test_generator_units_list_has_exactly_the_three_units():
    text = _generator_text()
    assert "mlx-supervisor" not in text
    for unit in (
        "com.fagan.pipeline.advance-scheduler.service",
        "com.fagan.pipeline.usage-poller.service",
        "com.fagan.pipeline.usage-poller.timer",
    ):
        assert unit in text, f"generator must render {unit}"


def test_generator_default_out_dir_is_repo_root_systemd():
    text = _generator_text()
    assert 'OUT_DIR="${REPO_ROOT}/systemd"' in text, (
        "--out-dir must default to the RESOLVED repo root's systemd/ dir"
    )


def test_generator_default_out_dir_is_computed_after_flag_parsing():
    text = _generator_text()
    loop_idx = text.index("while [[ $# -gt 0 ]]")
    default_idx = text.index('OUT_DIR="${REPO_ROOT}/systemd"')
    assert default_idx > loop_idx, (
        "the --out-dir default must be computed AFTER flag parsing, otherwise "
        "--repo-root without --out-dir would write into this repo's own systemd/"
    )


def test_generator_creates_output_directory():
    text = _generator_text()
    assert 'mkdir -p "$OUT_DIR"' in text


def test_generator_renders_logrotate_conf_from_logrotate_template():
    text = _generator_text()
    assert "pipeline-logs.logrotate.template" in text
    assert "pipeline-logs.logrotate.conf" in text


# ---------- templates (preconditions) ----------

def test_all_four_templates_exist():
    for name in _TEMPLATES:
        path = _SYSTEMD / name
        assert path.is_file(), f"missing template {path}"


def test_templates_use_only_documented_placeholder_tokens():
    allowed = {"REPO_ROOT", "HOME"}
    for name in _TEMPLATES:
        path = _SYSTEMD / name
        found = set(_TOKEN_RE.findall(path.read_text()))
        assert found <= allowed, (
            f"{path.name} uses undocumented placeholder token(s): {found - allowed}"
        )


def test_only_service_templates_use_home_token():
    for name in _TEMPLATES:
        path = _SYSTEMD / name
        text = path.read_text()
        if name.endswith(".service.template"):
            assert "{{HOME}}" in text, f"{path.name} should carry a {{{{HOME}}}} token"
        else:
            assert "{{HOME}}" not in text, (
                f"{path.name} must not carry a {{{{HOME}}}} token"
            )


# ---------- CLI: happy path ----------

def test_generates_exactly_the_four_expected_output_files(generator, fake_repo_and_out):
    fake_repo, out_dir = fake_repo_and_out
    _run_generator(["--repo-root", str(fake_repo), "--out-dir", str(out_dir)])
    produced = {p.name for p in out_dir.iterdir()}
    assert produced == _EXPECTED_OUTPUTS, (
        f"expected exactly {sorted(_EXPECTED_OUTPUTS)}, got {sorted(produced)}"
    )


def test_no_output_file_retains_a_template_suffix(generator, fake_repo_and_out):
    fake_repo, out_dir = fake_repo_and_out
    _run_generator(["--repo-root", str(fake_repo), "--out-dir", str(out_dir)])
    for path in out_dir.iterdir():
        assert not path.name.endswith(".template"), (
            f"{path.name} must be rendered without the .template suffix"
        )


def test_output_files_are_non_empty(generator, fake_repo_and_out):
    fake_repo, out_dir = fake_repo_and_out
    _run_generator(["--repo-root", str(fake_repo), "--out-dir", str(out_dir)])
    for name in _EXPECTED_OUTPUTS:
        path = out_dir / name
        assert path.is_file(), f"missing rendered output {path}"
        assert path.read_text().strip(), f"{path.name} must not be empty"


def test_repo_root_token_is_replaced_in_every_output(generator, fake_repo_and_out):
    fake_repo, out_dir = fake_repo_and_out
    _run_generator(["--repo-root", str(fake_repo), "--out-dir", str(out_dir)])
    for name in _EXPECTED_OUTPUTS:
        text = (out_dir / name).read_text()
        assert "{{REPO_ROOT}}" not in text, (
            f"{name} still contains an unrendered {{{{REPO_ROOT}}}} token"
        )
        # Only files whose template actually carried the token can be expected
        # to contain the substituted value (the .timer template has none).
        template = _SYSTEMD / _TEMPLATE_FOR_OUTPUT[name]
        if "{{REPO_ROOT}}" in template.read_text():
            assert str(fake_repo) in text, (
                f"{name} must contain the actual --repo-root value {fake_repo}"
            )


def test_home_token_is_replaced_in_the_two_service_files(generator, fake_repo_and_out, fake_home_env):
    fake_repo, out_dir = fake_repo_and_out
    fake_home, env = fake_home_env
    _run_generator(
        ["--repo-root", str(fake_repo), "--out-dir", str(out_dir)], env=env
    )
    for name in _SERVICE_OUTPUTS:
        text = (out_dir / name).read_text()
        assert "{{HOME}}" not in text, (
            f"{name} still contains an unrendered {{{{HOME}}}} token"
        )
        assert str(fake_home) in text, (
            f"{name} must contain the runtime $HOME value {fake_home}"
        )


def test_home_token_is_replaced_with_the_real_home_env_value(generator, fake_repo_and_out):
    real_home = os.environ.get("HOME")
    if not real_home:
        pytest.skip("HOME is not set in this environment")
    fake_repo, out_dir = fake_repo_and_out
    _run_generator(["--repo-root", str(fake_repo), "--out-dir", str(out_dir)])
    for name in _SERVICE_OUTPUTS:
        text = (out_dir / name).read_text()
        assert "{{HOME}}" not in text
        assert real_home in text, (
            f"{name} must contain the real $HOME value {real_home}"
        )


def test_timer_and_logrotate_outputs_have_no_home_token(generator, fake_repo_and_out):
    fake_repo, out_dir = fake_repo_and_out
    _run_generator(["--repo-root", str(fake_repo), "--out-dir", str(out_dir)])
    for name in _NON_SERVICE_OUTPUTS:
        text = (out_dir / name).read_text()
        assert "{{HOME}}" not in text, (
            f"{name} must not contain a {{{{HOME}}}} token"
        )


def test_no_output_file_retains_any_placeholder_token(generator, fake_repo_and_out):
    fake_repo, out_dir = fake_repo_and_out
    _run_generator(["--repo-root", str(fake_repo), "--out-dir", str(out_dir)])
    for name in _EXPECTED_OUTPUTS:
        leftovers = set(_TOKEN_RE.findall((out_dir / name).read_text()))
        assert not leftovers, f"{name} has unrendered token(s): {leftovers}"


def test_logrotate_output_is_named_conf_not_template(generator, fake_repo_and_out):
    fake_repo, out_dir = fake_repo_and_out
    _run_generator(["--repo-root", str(fake_repo), "--out-dir", str(out_dir)])
    assert (out_dir / "pipeline-logs.logrotate.conf").is_file()
    assert not (out_dir / "pipeline-logs.logrotate.template").exists()


# ---------- CLI: defaults ----------

def test_out_dir_defaults_to_repo_root_systemd(generator, tmp_path):
    fake_repo = tmp_path / "fake-repo"
    fake_repo.mkdir()
    _run_generator(["--repo-root", str(fake_repo)])
    produced = {p.name for p in (fake_repo / "systemd").iterdir()}
    assert produced == _EXPECTED_OUTPUTS, (
        "omitting --out-dir must write into <repo-root>/systemd"
    )


def test_out_dir_default_does_not_write_into_this_repo(generator, tmp_path):
    # Guard against the regression the launchd generator's comment calls out:
    # a caller passing --repo-root without --out-dir must NOT silently write
    # into this repo's own committed systemd/ directory.
    fake_repo = tmp_path / "fake-repo"
    fake_repo.mkdir()
    before = {p.name: p.stat().st_mtime_ns for p in _SYSTEMD.iterdir()}
    _run_generator(["--repo-root", str(fake_repo)])
    after = {p.name: p.stat().st_mtime_ns for p in _SYSTEMD.iterdir()}
    assert before == after, (
        "--repo-root without --out-dir must not touch this repo's own systemd/ dir"
    )


def test_repo_root_defaults_to_the_scripts_own_repo_root(generator, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _run_generator(["--out-dir", str(out_dir)])
    text = (out_dir / "com.fagan.pipeline.advance-scheduler.service").read_text()
    match = re.search(r"^WorkingDirectory=(.+)$", text, re.MULTILINE)
    assert match, "rendered service must contain a WorkingDirectory= line"
    rendered_root = Path(match.group(1).strip())
    assert rendered_root.resolve() == _REPO.resolve(), (
        "omitting --repo-root must default to the script's own repository root "
        f"({_REPO}), got {rendered_root}"
    )
    assert rendered_root.resolve() != out_dir.resolve()


def test_empty_out_dir_falls_back_to_repo_root_systemd(generator, tmp_path):
    fake_repo = tmp_path / "fake-repo"
    fake_repo.mkdir()
    _run_generator(["--repo-root", str(fake_repo), "--out-dir", ""])
    produced = {p.name for p in (fake_repo / "systemd").iterdir()}
    assert produced == _EXPECTED_OUTPUTS, (
        "an empty --out-dir value must fall back to <repo-root>/systemd"
    )


# ---------- CLI: negative / boundary ----------

def test_unknown_option_fails_with_message(generator, fake_repo_and_out):
    fake_repo, out_dir = fake_repo_and_out
    result = _run_generator(
        ["--repo-root", str(fake_repo), "--out-dir", str(out_dir), "--bogus"],
        check=False,
    )
    assert result.returncode != 0, "an unknown option must exit non-zero"
    assert "Unknown option" in result.stderr, (
        f"stderr should name the unknown option, got: {result.stderr!r}"
    )


def test_missing_template_fails_closed(generator, tmp_path):
    # Copy the generator into a temp "repo" with no systemd/ templates: the
    # script must fail (set -e + sed on a missing source) rather than silently
    # producing nothing or an empty file.
    fake_repo = tmp_path / "fake-repo"
    (fake_repo / "scripts").mkdir(parents=True)
    copied = fake_repo / "scripts" / "generate_systemd_units.sh"
    copied.write_text(_generator_text())
    copied.chmod(copied.stat().st_mode | stat.S_IXUSR)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    result = subprocess.run(
        [str(copied), "--repo-root", str(fake_repo), "--out-dir", str(out_dir)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0, (
        "generator must fail closed when a template is missing"
    )
    # `sed ... > "$dst"` may leave a zero-byte file behind before failing; what
    # matters is that no *rendered* content is produced.
    leftover = out_dir / "com.fagan.pipeline.advance-scheduler.service"
    assert not leftover.exists() or not leftover.read_text().strip(), (
        "no rendered output should be produced when its template is missing"
    )


def test_generator_does_not_require_mlx_model_path(generator, fake_repo_and_out):
    # Unlike the launchd generator, this one has no fail-closed MLX check: it
    # must succeed with MLX_MODEL_PATH unset and no --mlx-model-path flag.
    fake_repo, out_dir = fake_repo_and_out
    env = {k: v for k, v in os.environ.items() if k != "MLX_MODEL_PATH"}
    result = _run_generator(
        ["--repo-root", str(fake_repo), "--out-dir", str(out_dir)],
        env=env, check=False,
    )
    assert result.returncode == 0, (
        f"generator must not require MLX_MODEL_PATH; stderr: {result.stderr!r}"
    )
    assert {p.name for p in out_dir.iterdir()} == _EXPECTED_OUTPUTS


def test_launchd_generator_is_untouched_by_this_story():
    # The systemd generator is a structural mirror; the launchd generator must
    # still exist and still carry its own MLX flag.
    assert _LAUNCHD_GENERATOR.is_file()
    text = _LAUNCHD_GENERATOR.read_text()
    assert "--mlx-model-path" in text
    assert "mlx-supervisor" in text
