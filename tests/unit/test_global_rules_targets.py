"""Tests for pipeline.global_rules_targets (global instruction-file targets).

The module is pure: every function takes a synthetic ``env`` Mapping and
returns a Path. Nothing here touches the real environment or the real
filesystem - HOME always comes from ``env['HOME']``.

This story adds ONE module; later sibling stories add other modules under
pipeline/ and scripts/ for the same feature, so nothing here asserts a
total count or the exact contents of any shared artifact.
"""
import importlib
from pathlib import Path

import pytest

MODULE = "pipeline.global_rules_targets"
REPO_ROOT = Path(__file__).resolve().parents[2]

# tool -> (env, expected instructions_path) for the documented defaults.
DEFAULTS = {
    "claude": ({"HOME": "/home/u"}, Path("/home/u/.claude/CLAUDE.md")),
    "codex": ({"HOME": "/home/u"}, Path("/home/u/.codex/AGENTS.md")),
    "opencode": ({"HOME": "/home/u"}, Path("/home/u/.config/opencode/AGENTS.md")),
}


def _mod():
    return importlib.import_module(MODULE)


def test_supported_tools_membership():
    tools = _mod().SUPPORTED_TOOLS
    assert isinstance(tools, tuple)
    for name in ("claude", "codex", "opencode"):
        assert name in tools


@pytest.mark.parametrize("tool", sorted(DEFAULTS))
def test_instructions_path_default(tool):
    env, expected = DEFAULTS[tool]
    got = _mod().instructions_path(tool, env)
    assert isinstance(got, Path)
    assert got == expected


def test_claude_config_dir_override():
    got = _mod().instructions_path(
        "claude", {"HOME": "/home/u", "CLAUDE_CONFIG_DIR": "/cfg/claude"}
    )
    assert got == Path("/cfg/claude/CLAUDE.md")


def test_codex_home_override():
    got = _mod().instructions_path(
        "codex", {"HOME": "/home/u", "CODEX_HOME": "/cfg/codex"}
    )
    assert got == Path("/cfg/codex/AGENTS.md")


def test_xdg_config_home_override_for_opencode():
    got = _mod().instructions_path(
        "opencode", {"HOME": "/home/u", "XDG_CONFIG_HOME": "/xdg"}
    )
    assert got == Path("/xdg/opencode/AGENTS.md")


def test_opencode_config_dir_outranks_xdg():
    got = _mod().instructions_path(
        "opencode",
        {
            "HOME": "/home/u",
            "XDG_CONFIG_HOME": "/xdg",
            "OPENCODE_CONFIG_DIR": "/cfg/opencode",
        },
    )
    assert got == Path("/cfg/opencode/AGENTS.md")


@pytest.mark.parametrize("tool", sorted(DEFAULTS))
def test_rules_dir_is_sibling_of_instructions_path(tool):
    env, _ = DEFAULTS[tool]
    mod = _mod()
    assert mod.rules_dir(tool, env) == mod.instructions_path(tool, env).parent / "fagan-rules"


def test_rules_dir_follows_override():
    got = _mod().rules_dir("codex", {"HOME": "/home/u", "CODEX_HOME": "/cfg/codex"})
    assert got == Path("/cfg/codex/fagan-rules")


def test_shadowing_path_codex_is_agents_override():
    got = _mod().shadowing_path("codex", {"HOME": "/home/u"})
    assert got == Path("/home/u/.codex/AGENTS.override.md")


def test_shadowing_path_codex_follows_codex_home():
    got = _mod().shadowing_path("codex", {"HOME": "/home/u", "CODEX_HOME": "/cfg/codex"})
    assert got == Path("/cfg/codex/AGENTS.override.md")


@pytest.mark.parametrize("tool", ["claude", "opencode"])
def test_shadowing_path_none_for_claude_and_opencode(tool):
    env = {"HOME": "/home/u", "CLAUDE_CONFIG_DIR": "/cfg/claude", "OPENCODE_CONFIG_DIR": "/cfg/oc"}
    assert _mod().shadowing_path(tool, env) is None


def test_shadowing_path_is_pure_and_does_not_check_existence():
    # The path does not exist on disk; a pure function still returns it.
    got = _mod().shadowing_path("codex", {"HOME": "/nonexistent-home-xyz"})
    assert got == Path("/nonexistent-home-xyz/.codex/AGENTS.override.md")
    assert not got.exists()


def test_unknown_tool_rejected_naming_supported_tools():
    mod = _mod()
    for func in (mod.instructions_path, mod.rules_dir, mod.shadowing_path):
        with pytest.raises(ValueError) as exc:
            func("cursor", {"HOME": "/home/u"})
        msg = str(exc.value)
        for name in ("claude", "codex", "opencode"):
            assert name in msg


def test_empty_tool_rejected():
    with pytest.raises(ValueError):
        _mod().instructions_path("", {"HOME": "/home/u"})


def test_case_sensitive_tool_rejected():
    with pytest.raises(ValueError):
        _mod().instructions_path("Claude", {"HOME": "/home/u"})


@pytest.mark.parametrize("tool", sorted(DEFAULTS))
def test_missing_home_rejected(tool):
    with pytest.raises(ValueError):
        _mod().instructions_path(tool, {})


@pytest.mark.parametrize("tool", sorted(DEFAULTS))
def test_empty_home_rejected(tool):
    with pytest.raises(ValueError):
        _mod().instructions_path(tool, {"HOME": ""})


@pytest.mark.parametrize(
    "tool,key",
    [("codex", "CODEX_HOME"), ("claude", "CLAUDE_CONFIG_DIR"), ("opencode", "OPENCODE_CONFIG_DIR")],
)
def test_relative_override_rejected(tool, key):
    with pytest.raises(ValueError):
        _mod().instructions_path(tool, {"HOME": "/home/u", key: "rel"})


def test_empty_override_is_treated_as_unset():
    mod = _mod()
    assert mod.instructions_path("codex", {"HOME": "/home/u", "CODEX_HOME": ""}) == Path(
        "/home/u/.codex/AGENTS.md"
    )
    assert mod.instructions_path(
        "opencode", {"HOME": "/home/u", "XDG_CONFIG_HOME": "", "OPENCODE_CONFIG_DIR": ""}
    ) == Path("/home/u/.config/opencode/AGENTS.md")
    assert mod.instructions_path("claude", {"HOME": "/home/u", "CLAUDE_CONFIG_DIR": ""}) == Path(
        "/home/u/.claude/CLAUDE.md"
    )


@pytest.mark.parametrize(
    "tool,key",
    [("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"), ("opencode", "OPENCODE_CONFIG_DIR")],
)
def test_override_removes_the_need_for_home(tool, key):
    got = _mod().instructions_path(tool, {key: "/cfg/x"})
    assert got.parent == Path("/cfg/x")


def test_home_comes_from_env_not_os_environ(monkeypatch):
    monkeypatch.setenv("HOME", "/real/home")
    got = _mod().instructions_path("claude", {"HOME": "/stub/home"})
    assert got == Path("/stub/home/.claude/CLAUDE.md")


def test_module_source_has_no_io_and_no_os_environ():
    src = (REPO_ROOT / "pipeline" / "global_rules_targets.py").read_text(encoding="utf-8")
    assert "Path.home(" not in src
    assert "os.environ" not in src
