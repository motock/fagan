"""Tests for the thin scripts/configure.py CLI."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "configure.py"

ALL_FLAGS = (
    "--repo-root", "--data-dir", "--port", "--autonomy", "--role", "--provider",
    "--model", "--set", "--skip-notifications", "--skip-integrations",
    "--skip-services", "--skip-claude-assets", "--yes", "--dry-run",
)


def _run(args, cwd, home, python_args=(), extra_env=None):
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "PIPELINE_SKIP_ENV_FILE": "1",
    }
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, *python_args, str(SCRIPT), *args],
        cwd=cwd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False,
        timeout=60,
    )


@pytest.fixture
def temp_repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    registry = {
        "providers": {"claude": {"models": {"sonnet": {}}}},
        "roles": {},
    }
    (repo / "model_registry.json").write_text(json.dumps(registry))
    return repo


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    return path


def _tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def test_should_print_help_without_import_errors(tmp_path, home):
    result = _run(["--help"], tmp_path, home)

    assert result.returncode == 0
    assert "ModuleNotFoundError" not in result.stderr


def test_should_list_every_flag_in_help(tmp_path, home):
    result = _run(["--help"], tmp_path, home)

    missing = [flag for flag in ALL_FLAGS if flag not in result.stdout]
    assert missing == []


def test_should_exit_2_on_unknown_flag(tmp_path, home):
    result = _run(["--bogus"], tmp_path, home)

    assert result.returncode == 2


def test_should_forward_repeated_set_flags_to_build_plan(monkeypatch, tmp_path):
    from pipeline.setup import wizard
    from scripts import configure

    seen = {}

    def fake_build_plan(args, **kwargs):
        seen["set"] = list(args.set)
        return {"errors": ["stop here"]}

    monkeypatch.setattr(wizard, "build_plan", fake_build_plan)
    monkeypatch.chdir(tmp_path)

    code = configure.main(["--set", "A=1", "--set", "B=2", "--yes"])

    assert (code, seen["set"]) == (2, ["A=1", "B=2"])


def test_should_default_set_to_empty_list():
    from scripts import configure

    assert configure.build_parser().parse_args([]).set == []


def test_should_default_port_to_8001_and_leave_repo_root_unset():
    from scripts import configure

    args = configure.build_parser().parse_args([])

    assert (args.port, args.repo_root) == (8001, None)


def test_should_create_nothing_on_dry_run(temp_repo, home):
    before = _tree(temp_repo)

    result = _run(["--dry-run", "--yes", "--repo-root", str(temp_repo)], temp_repo, home)

    assert (result.returncode, _tree(temp_repo)) == (0, before)


def test_should_print_a_plan_on_dry_run(temp_repo, home):
    result = _run(["--dry-run", "--yes", "--repo-root", str(temp_repo)], temp_repo, home)

    assert "PIPELINE_" in result.stdout or "REPO_ROOT" in result.stdout


def test_should_write_env_file_on_real_run(temp_repo, home):
    result = _run(
        ["--yes", "--repo-root", str(temp_repo), "--skip-services",
         "--skip-claude-assets", "--data-dir", str(temp_repo / "data")],
        temp_repo, home,
    )

    assert (result.returncode, (temp_repo / ".pipeline.env").is_file()) == (0, True)


def test_should_create_planned_data_directories(temp_repo, home):
    data = temp_repo / "data"
    _run(
        ["--yes", "--repo-root", str(temp_repo), "--skip-services",
         "--skip-claude-assets", "--data-dir", str(data)],
        temp_repo, home,
    )

    assert data.is_dir()


def test_should_exit_2_and_write_nothing_when_set_lacks_equals(temp_repo, home):
    before = _tree(temp_repo)

    result = _run(["--yes", "--set", "NOEQUALS", "--repo-root", str(temp_repo)],
                  temp_repo, home)

    assert (result.returncode, _tree(temp_repo)) == (2, before)


def test_should_exit_2_when_provider_given_without_role(temp_repo, home):
    before = _tree(temp_repo)

    result = _run(["--yes", "--provider", "claude", "--repo-root", str(temp_repo)],
                  temp_repo, home)

    assert (result.returncode, _tree(temp_repo)) == (2, before)


def test_should_point_at_install_sh_when_deps_missing(tmp_path, home):
    # -S drops site-packages, so `import mcp, httpx` fails like a venv-less host.
    result = _run([], tmp_path, home, python_args=("-S",))

    assert result.returncode == 2
    assert "run scripts/install.sh first" in result.stderr


def test_should_return_130_on_keyboard_interrupt(monkeypatch, tmp_path):
    from pipeline.setup import detect
    from scripts import configure

    def boom(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(detect, "collect_state", boom)
    monkeypatch.chdir(tmp_path)

    assert configure.main(["--yes"]) == 130


def test_should_return_1_naming_only_exception_type(monkeypatch, tmp_path, capsys):
    from pipeline.setup import detect
    from scripts import configure

    def boom(*_a, **_k):
        raise RuntimeError("secret-value-123")

    monkeypatch.setattr(detect, "collect_state", boom)
    monkeypatch.chdir(tmp_path)

    code = configure.main(["--yes"])

    captured = capsys.readouterr()
    assert code == 1
    assert "RuntimeError" in captured.err
    assert "secret-value-123" not in captured.err + captured.out
    assert "Traceback" not in captured.err


def test_should_return_0_on_eof(monkeypatch, tmp_path):
    from pipeline.setup import detect
    from scripts import configure

    def boom(*_a, **_k):
        raise EOFError

    monkeypatch.setattr(detect, "collect_state", boom)
    monkeypatch.chdir(tmp_path)

    assert configure.main(["--yes"]) == 0


def test_should_not_import_pipeline_at_module_top_level():
    source = SCRIPT.read_text()
    top = source.split("def main", 1)[0]
    import_lines = [ln for ln in top.splitlines() if ln.startswith(("import ", "from "))]

    assert [ln for ln in import_lines if "pipeline" in ln] == []
