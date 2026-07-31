"""Tests for pipeline.build_detect.detect_lint_command.

TDD-first (Mode 40 follow-up): the original lint-gate story's brief
asserted this repo has a `[tool.ruff]`/`ruff.toml` config, which is FALSE -
this repo lints via a bare `ruff check .` in CI with ruff declared only in
requirements-dev.txt and no config file at all. A config-file-only
detection premise misses this repo's own real signal entirely. These tests
pin the corrected, broader detection: an explicit lint config OR a
declared-dependency + available-tool signal, always fail-open when the
signal fires but the tool itself isn't actually runnable.
"""
import shutil
from pathlib import Path

from pipeline.build_detect import detect_lint_command


def _make_venv_with_ruff(venv_dir: Path) -> None:
    bin_dir = venv_dir / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("#!/bin/sh\n")
    (bin_dir / "python").chmod(0o755)
    (bin_dir / "ruff").write_text("#!/bin/sh\n")
    (bin_dir / "ruff").chmod(0o755)


class TestNoSignal:
    def test_empty_dir_returns_none(self, tmp_path):
        assert detect_lint_command(tmp_path) is None

    def test_pyproject_without_ruff_signal_returns_none(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        assert detect_lint_command(tmp_path) is None


class TestRuffConfigFile:
    def test_ruff_toml_with_venv_ruff_uses_venv_binary(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        (tmp_path / "ruff.toml").write_text("line-length = 100\n")
        _make_venv_with_ruff(tmp_path / ".venv")
        result = detect_lint_command(tmp_path)
        assert result is not None
        cwd, cmd = result
        assert cwd == tmp_path
        assert cmd == [str(tmp_path / ".venv" / "bin" / "ruff"), "check", "."]

    def test_dot_ruff_toml_is_also_recognized(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        (tmp_path / ".ruff.toml").write_text("line-length = 100\n")
        _make_venv_with_ruff(tmp_path / ".venv")
        assert detect_lint_command(tmp_path) is not None

    def test_pyproject_tool_ruff_section_is_recognized(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'x'\n\n[tool.ruff]\nline-length = 100\n"
        )
        _make_venv_with_ruff(tmp_path / ".venv")
        assert detect_lint_command(tmp_path) is not None

    def test_no_venv_ruff_falls_back_to_which(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'x'\n\n[tool.ruff]\n"
        )
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/ruff" if name == "ruff" else None)
        result = detect_lint_command(tmp_path)
        assert result == (tmp_path, ["/usr/local/bin/ruff", "check", "."])

    def test_config_present_but_tool_unavailable_fails_open(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'x'\n\n[tool.ruff]\n"
        )
        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert detect_lint_command(tmp_path) is None


class TestRuffDeclaredAsDependency:
    """The signal this repo itself actually emits: no config file, ruff
    named in requirements-dev.txt, bare `ruff check .` enforced in CI."""

    def test_requirements_dev_naming_ruff_plus_venv_binary_is_detected(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        (tmp_path / "requirements-dev.txt").write_text("ruff>=0.15.22\npytest\n")
        _make_venv_with_ruff(tmp_path / ".venv")
        result = detect_lint_command(tmp_path)
        assert result is not None
        _, cmd = result
        assert cmd[0].endswith("ruff")
        assert cmd[1:] == ["check", "."]

    def test_requirements_txt_naming_ruff_is_also_recognized(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        (tmp_path / "requirements.txt").write_text("ruff\n")
        _make_venv_with_ruff(tmp_path / ".venv")
        assert detect_lint_command(tmp_path) is not None

    def test_dependency_declared_but_no_tool_anywhere_fails_open(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        (tmp_path / "requirements-dev.txt").write_text("ruff>=0.15.22\n")
        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert detect_lint_command(tmp_path) is None

    def test_setup_py_project_also_recognized(self, tmp_path):
        (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")
        (tmp_path / "requirements-dev.txt").write_text("ruff\n")
        _make_venv_with_ruff(tmp_path / ".venv")
        assert detect_lint_command(tmp_path) is not None

    def test_ruff_mentioned_only_in_unrelated_text_file_is_not_a_signal(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        (tmp_path / "README.md").write_text("We use ruff for linting.\n")
        _make_venv_with_ruff(tmp_path / ".venv")
        assert detect_lint_command(tmp_path) is None


class TestEslint:
    def test_eslint_config_js_plus_package_json_detected(self, tmp_path):
        (tmp_path / "package.json").write_text("{}\n")
        (tmp_path / "eslint.config.js").write_text("module.exports = [];\n")
        result = detect_lint_command(tmp_path)
        assert result == (tmp_path, ["npx", "--no-install", "eslint", "."])

    def test_dot_eslintrc_json_is_recognized(self, tmp_path):
        (tmp_path / "package.json").write_text("{}\n")
        (tmp_path / ".eslintrc.json").write_text("{}\n")
        assert detect_lint_command(tmp_path) is not None

    def test_eslint_config_without_package_json_is_not_a_signal(self, tmp_path):
        (tmp_path / "eslint.config.js").write_text("module.exports = [];\n")
        assert detect_lint_command(tmp_path) is None


class TestGolangci:
    def test_golangci_yml_plus_available_tool_detected(self, tmp_path, monkeypatch):
        (tmp_path / ".golangci.yml").write_text("run:\n  timeout: 5m\n")
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/golangci-lint" if name == "golangci-lint" else None)
        result = detect_lint_command(tmp_path)
        assert result == (tmp_path, ["/usr/local/bin/golangci-lint", "run"])

    def test_golangci_yaml_variant_is_recognized(self, tmp_path, monkeypatch):
        (tmp_path / ".golangci.yaml").write_text("run:\n  timeout: 5m\n")
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/golangci-lint" if name == "golangci-lint" else None)
        assert detect_lint_command(tmp_path) is not None

    def test_golangci_config_but_tool_unavailable_fails_open(self, tmp_path, monkeypatch):
        (tmp_path / ".golangci.yml").write_text("run:\n  timeout: 5m\n")
        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert detect_lint_command(tmp_path) is None


class TestLivePipelineRepo:
    """This repo's own shape: no standalone ruff.toml/.ruff.toml, only a
    narrow [tool.ruff.lint.per-file-ignores] section in pyproject.toml (an
    exemption for read-only acceptance fixtures, which can't be edited to
    fix style nits without failing the merge gate's tamper check), plus
    ruff pinned in requirements-dev.txt. CI runs bare `ruff check .` either
    way -- detect_lint_command's command shape doesn't change based on
    whether detection came from the config-file signal or the
    declared-dependency signal, so this still doubles as the live proof
    the story's original premise was checking for and got wrong."""

    def test_detects_this_repos_own_lint_setup(self):
        repo_root = Path(__file__).resolve().parent.parent.parent
        assert not (repo_root / "ruff.toml").exists()
        assert not (repo_root / ".ruff.toml").exists()
        pyproject_text = (repo_root / "pyproject.toml").read_text()
        assert "[tool.ruff.lint.per-file-ignores]" in pyproject_text
        result = detect_lint_command(repo_root)
        assert result is not None
        _, cmd = result
        assert cmd[-2:] == ["check", "."]
        assert "ruff" in cmd[0]
