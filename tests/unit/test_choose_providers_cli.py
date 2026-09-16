"""Tests for scripts/choose_providers.py, the interactive role-routing picker.

The script is driven through its injection seam::

    main(argv=None, *, input_fn=input, setter=None,
         effective_config=None, registry=None) -> int

``input_fn`` replaces stdin, ``setter`` replaces
``PipelineService.set_role_default``, and ``effective_config`` / ``registry``
replace the on-disk config and model registry.  No test here touches real
stdin or writes ``model_registry.json``.

The script must decide "is stdin a TTY?" via ``sys.stdin.isatty()``; the
``tty`` / ``notty`` fixtures patch both that and ``os.isatty`` so either
spelling is honoured.
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline.config_provenance import PIPELINE_ROLES
from pipeline.provider_choice import build_choice_model

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "choose_providers.py"
REGISTRY_PATH = REPO_ROOT / "model_registry.json"

ROLE_A, ROLE_B = PIPELINE_ROLES[0], PIPELINE_ROLES[1]
N_ROLES = len(PIPELINE_ROLES)

REGISTRY = {
    "providers": {
        "claude": {"models": {"opus": {}, "sonnet": {}}},
        "ollama": {"models": {"llama3": {}}},
    },
    "roles": {},
}

EFFECTIVE = {
    "roles": [
        {
            "role": ROLE_A,
            "provider": "claude",
            "model": "opus",
            "provider_source": "registry",
            "model_source": "registry",
            "error": None,
        },
        {
            "role": ROLE_B,
            "provider": "ollama",
            "model": "llama3",
            "provider_source": "env",
            "model_source": "env",
            "error": None,
        },
    ]
}

# Options are the registry's declared pairs, sorted by (provider, model).
OPTIONS = build_choice_model(EFFECTIVE, REGISTRY)[0]["options"]
OPTION_2 = OPTIONS[1]


def _load_module():
    spec = importlib.util.spec_from_file_location("choose_providers", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


class _FakeStdin:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty

    def fileno(self):
        return 0


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdin(True))
    monkeypatch.setattr(os, "isatty", lambda fd: True)


@pytest.fixture
def notty(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdin(False))
    monkeypatch.setattr(os, "isatty", lambda fd: False)


class FakeInput:
    """Stands in for ``input``; records prompts and replays canned answers."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError("input_fn called more times than expected")
        return self.answers.pop(0)


class FakeSetter:
    """Stands in for ``PipelineService.set_role_default``."""

    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    def __call__(self, role, provider, model):
        self.calls.append((role, provider, model))
        if self.results:
            return self.results.pop(0)
        return {"ok": True}


def _answers(first="", rest=""):
    """One answer per pipeline role, in PIPELINE_ROLES order."""
    return [first] + [rest] * (N_ROLES - 1)


def _run(mod, argv=(), answers=(), setter=None):
    inp = FakeInput(answers)
    st = setter if setter is not None else FakeSetter()
    code = mod.main(
        list(argv),
        input_fn=inp,
        setter=st,
        effective_config=EFFECTIVE,
        registry=REGISTRY,
    )
    return code, inp, st


# --- injection seam -------------------------------------------------------


def test_main_exposes_input_and_setter_injection(mod):
    params = inspect.signature(mod.main).parameters
    assert "input_fn" in params
    assert "setter" in params
    assert params["input_fn"].default is not inspect.Parameter.empty
    assert params["setter"].default is not inspect.Parameter.empty


# --- positive path --------------------------------------------------------


def test_choosing_option_calls_setter_once(mod, tty):
    code, inp, st = _run(mod, answers=_answers(first="2"))
    assert code == 0
    assert st.calls == [(ROLE_A, OPTION_2["provider"], OPTION_2["model"])]
    assert len(inp.prompts) == N_ROLES


def test_enter_keeps_current_and_calls_setter_zero_times(mod, tty):
    code, inp, st = _run(mod, answers=_answers())
    assert code == 0
    assert st.calls == []
    assert len(inp.prompts) == N_ROLES


def test_dry_run_calls_setter_zero_times(mod, tty):
    before = (REGISTRY_PATH.stat().st_mtime_ns, REGISTRY_PATH.read_bytes())
    code, _inp, st = _run(mod, argv=["--dry-run"], answers=_answers(first="2", rest="1"))
    assert code == 0
    assert st.calls == []
    after = (REGISTRY_PATH.stat().st_mtime_ns, REGISTRY_PATH.read_bytes())
    assert after == before


# --- negative paths -------------------------------------------------------


def test_setter_failure_prints_error_and_continues(mod, tty, capsys):
    setter = FakeSetter(results=[{"ok": False, "error": "boom"}])
    code, inp, st = _run(mod, answers=_answers(first="2"), setter=setter)
    out = capsys.readouterr().out
    assert code == 0
    assert "boom" in out
    # The later role is still offered rather than the run aborting.
    assert len(inp.prompts) == N_ROLES
    assert ROLE_B in out
    assert st.calls == [(ROLE_A, OPTION_2["provider"], OPTION_2["model"])]


def test_invalid_input_reprompts_instead_of_writing(mod, tty):
    code, inp, st = _run(
        mod,
        argv=["--role", ROLE_A],
        answers=["abc", "99", "0", "2"],
    )
    assert code == 0
    assert len(inp.prompts) == 4
    assert st.calls == [(ROLE_A, OPTION_2["provider"], OPTION_2["model"])]


# --- non-interactive safety ----------------------------------------------


def test_non_tty_prints_routing_and_exits_zero(mod, notty, capsys):
    inp = FakeInput([])  # raises if the script ever tries to prompt
    st = FakeSetter()
    code = mod.main(
        [],
        input_fn=inp,
        setter=st,
        effective_config=EFFECTIVE,
        registry=REGISTRY,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert inp.prompts == []
    assert st.calls == []
    assert ROLE_A in out and ROLE_B in out
    assert "claude" in out and "opus" in out
    assert "model_registry.json" in out


# --- --role ---------------------------------------------------------------


def test_role_flag_offers_only_that_role(mod, tty):
    code, inp, st = _run(mod, argv=["--role", ROLE_A], answers=["2"])
    assert code == 0
    assert len(inp.prompts) == 1
    assert st.calls == [(ROLE_A, OPTION_2["provider"], OPTION_2["model"])]


def test_unknown_role_exits_nonzero_naming_valid_roles(mod, tty, capsys):
    inp = FakeInput([])
    st = FakeSetter()
    code = mod.main(
        ["--role", "not-a-real-role"],
        input_fn=inp,
        setter=st,
        effective_config=EFFECTIVE,
        registry=REGISTRY,
    )
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert code != 0
    assert st.calls == []
    assert inp.prompts == []
    for role in PIPELINE_ROLES:
        assert role in out


# --- rendering ------------------------------------------------------------


def test_renders_current_routing_and_provenance_source(mod, tty, capsys):
    _run(mod, answers=_answers())
    out = capsys.readouterr().out
    assert ROLE_A in out and ROLE_B in out
    assert "claude" in out and "opus" in out
    assert "ollama" in out and "llama3" in out
    assert "registry" in out  # provenance source for ROLE_A
    assert "env" in out  # provenance source for ROLE_B


def test_renders_numbered_options(mod, tty, capsys):
    _run(mod, argv=["--role", ROLE_A], answers=[""])
    out = capsys.readouterr().out
    for index, option in enumerate(OPTIONS, start=1):
        assert str(index) in out
        assert option["provider"] in out
        assert option["model"] in out


def test_summary_reports_changed_and_unchanged(mod, tty, capsys):
    _run(mod, answers=_answers(first="2"))
    out = capsys.readouterr().out.lower()
    assert "changed" in out
    assert "unchanged" in out
    assert ROLE_A.lower() in out
    assert ROLE_B.lower() in out


# --- persistence goes through set_role_default ----------------------------


def test_source_has_no_direct_persistence():
    src = SCRIPT_PATH.read_text()
    for forbidden in ("json.dump", "write_text", "os.replace"):
        assert forbidden not in src


def test_source_wires_config_registry_and_setter():
    src = SCRIPT_PATH.read_text()
    assert "build_choice_model" in src
    assert "get_effective_config" in src
    assert "load_registry" in src
    assert "set_role_default" in src


# --- real CLI (subprocess) ------------------------------------------------


def test_help_documents_dry_run_and_role():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "--dry-run" in proc.stdout
    assert "--role" in proc.stdout


def test_dry_run_subprocess_exits_zero_and_writes_nothing():
    before = (REGISTRY_PATH.stat().st_mtime_ns, REGISTRY_PATH.read_bytes())
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--dry-run"],
        input="\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert ROLE_A in proc.stdout
    after = (REGISTRY_PATH.stat().st_mtime_ns, REGISTRY_PATH.read_bytes())
    assert after == before
