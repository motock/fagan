"""TDD suite for the PP-04 authorization dimension in scripts/install_checks.py.

Written BEFORE the implementation (red). The base suite
(tests/unit/test_install_checks.py) pins two load-bearing facts that shape
how the authorization dimension is reached:

* ``collect_checks``'s exact parameter set is pinned to ``{"which",
  "py_version"}``, so a ``runner`` cannot be added as a third parameter
  without modifying that suite (forbidden);
* the base suite's happy-path test asserts ``ok`` for gh/claude/ollama with
  only ``which`` stubbed, so auth probing must be OFF by default (on a CI
  host gh is present-but-unauthenticated and claude/ollama are absent, which
  would flip those statuses and break the unmodified base suite).

The dimension is therefore reached through two module-level injectable
seams, both defaulting to ``None`` (presence-only, the base contract):

* ``ic._RUNNER`` — the CLI auth runner: ``runner(argv, timeout=seconds)``
  -> ``(returncode, stdout, stderr)``; used for the gh and claude probes;
* ``ic._URLOPEN`` — the ollama daemon HTTP seam (POST /api/me); the daemon
  probe is an HTTP call, not a subprocess, so it gets its own seam with the
  same guarantees (bounded timeout, never raises).

``main`` installs the real subprocess/urllib implementations for the
standalone run, so the shipped doctor does verify authorization while
direct ``collect_checks`` callers keep the presence-only default.

Every test in this file injects both seams (and a stub ``which``); no test
shells out for real and no test touches the live host. Probe commands
pinned here were resolved against the installed CLIs, not guessed:
``gh auth status`` (exit 0 = signed in) and ``claude auth status --json``
(non-interactive; JSON ``loggedIn`` boolean).
"""

import getpass
import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "install_checks.py"

_spec = importlib.util.spec_from_file_location("install_checks_pp04", str(_SCRIPT))
ic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ic)

_STUB_PATH_PREFIX = "/fake/toolchain/bin/"
_ALL_TOOLS = ("python", "git", "gh", "claude", "ollama", "docker")
_PROBED_TOOLS = ("gh", "claude", "ollama")
_PY_MIN = (3, 10)

# Resolved probe surfaces (verified non-interactive on the installed CLIs).
_GH_AUTH_ARGV = ("gh", "auth", "status")
_CLAUDE_AUTH_ARGV = ("claude", "auth", "status", "--json")


def _which_stub(present):
    """Return a shutil.which stand-in: fake path for names in ``present``."""

    def _which(name, *args, **kwargs):
        if name in present:
            return _STUB_PATH_PREFIX + name
        return None

    return _which


class _RunnerStub:
    """CLI runner seam stub: records ``(argv, timeout)``; scripted result."""

    def __init__(self, rc=0, stdout="", stderr="", exc=None):
        self.calls = []
        self._rc = rc
        self._stdout = stdout
        self._stderr = stderr
        self._exc = exc

    def __call__(self, argv, timeout):
        self.calls.append({"argv": tuple(argv), "timeout": timeout})
        if self._exc is not None:
            raise self._exc
        return self._rc, self._stdout, self._stderr


class _FakeResponse:
    """Minimal context-manager response carrying only a status code."""

    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _UrlopenStub:
    """Daemon HTTP seam stub: records ``(url, timeout)``; scripted status."""

    def __init__(self, status=200, exc=None):
        self.calls = []
        self._status = status
        self._exc = exc

    def __call__(self, request, timeout=None):
        self.calls.append(
            {"url": getattr(request, "full_url", str(request)), "timeout": timeout}
        )
        if self._exc is not None:
            raise self._exc
        return _FakeResponse(self._status)


def _collect(
    monkeypatch,
    present=None,
    runner=None,
    urlopen=None,
    py_version=_PY_MIN,
):
    """collect_checks with BOTH auth seams injected; the host is untouched."""
    if present is None:
        present = set(_ALL_TOOLS)
    monkeypatch.setattr(ic, "_RUNNER", runner, raising=False)
    monkeypatch.setattr(ic, "_URLOPEN", urlopen, raising=False)
    return ic.collect_checks(
        which=_which_stub(set(present) | {"python3"}), py_version=py_version
    )


def _by_name(checks):
    return {check["name"]: check for check in checks}


def _patch_collect(monkeypatch, checks):
    """Route main() through a fixed collect_checks result for determinism."""
    monkeypatch.setattr(
        ic, "collect_checks", lambda *args, **kwargs: [dict(c) for c in checks]
    )


# ---------------------------------------------------------------------------
# gh: required, present-but-unauthorized is distinct from missing
# ---------------------------------------------------------------------------


def test_gh_present_and_auth_status_exits_zero_reports_ok(monkeypatch):
    runner = _RunnerStub(rc=0)
    gh = _by_name(_collect(monkeypatch, runner=runner))["gh"]
    assert gh["status"] == "ok"
    assert gh["required"] is True
    assert runner.calls[0]["argv"] == _GH_AUTH_ARGV


def test_gh_present_and_auth_status_exits_nonzero_reports_unauthorized(monkeypatch):
    runner = _RunnerStub(rc=1, stderr="gh: not logged into any hosts")
    gh = _by_name(_collect(monkeypatch, runner=runner))["gh"]
    assert gh["status"] == "unauthorized"
    assert "gh auth login" in gh["hint"]


def test_gh_absent_reports_missing_with_install_hint_and_no_probe(monkeypatch):
    runner = _RunnerStub(rc=0)
    gh = _by_name(
        _collect(monkeypatch, present=set(_ALL_TOOLS) - {"gh"}, runner=runner)
    )["gh"]
    assert gh["status"] == "missing"
    assert "install" in gh["hint"].lower()
    assert not any(
        call["argv"] == _GH_AUTH_ARGV for call in runner.calls
    ), "an absent binary must not be probed"


# ---------------------------------------------------------------------------
# claude: resolved non-interactive surface, unknown when it cannot answer
# ---------------------------------------------------------------------------


def test_claude_present_and_signed_in_reports_ok(monkeypatch):
    runner = _RunnerStub(rc=0, stdout='{"loggedIn": true}')
    claude = _by_name(_collect(monkeypatch, runner=runner))["claude"]
    assert claude["status"] == "ok"
    assert runner.calls[1]["argv"] == _CLAUDE_AUTH_ARGV


def test_claude_present_and_logged_out_reports_unauthorized(monkeypatch):
    runner = _RunnerStub(rc=0, stdout='{"loggedIn": false}')
    claude = _by_name(_collect(monkeypatch, runner=runner))["claude"]
    assert claude["status"] == "unauthorized"
    assert "claude auth login" in claude["hint"]


def test_claude_auth_status_failing_reports_unknown_with_static_hint(monkeypatch):
    # An older CLI without the subcommand exits nonzero: that is not proof of
    # a signed-out state, so the status is unknown and the hint stays static.
    runner = _RunnerStub(rc=1, stderr="unknown command")
    claude = _by_name(_collect(monkeypatch, runner=runner))["claude"]
    assert claude["status"] == "unknown"
    assert "claude auth status" in claude["hint"]


# ---------------------------------------------------------------------------
# ollama: optional; only :cloud tags need the daemon's ollama.com sign-in
# ---------------------------------------------------------------------------


def test_ollama_present_daemon_not_signed_in_is_optional_unauthorized(monkeypatch):
    runner = _RunnerStub(rc=0)
    urlopen = _UrlopenStub(status=401)
    checks = _collect(monkeypatch, runner=runner, urlopen=urlopen)
    ollama = _by_name(checks)["ollama"]
    assert ollama["required"] is False
    assert ollama["status"] == "unauthorized"
    assert "ollama signin" in ollama["hint"]
    assert urlopen.calls[0]["timeout"] == ic._PROBE_TIMEOUT_SECONDS
    _patch_collect(monkeypatch, checks)
    assert ic.main([]) == 0, "an optional unauthorized tool never fails the run"


def test_ollama_present_and_signed_in_reports_ok(monkeypatch):
    urlopen = _UrlopenStub(status=200)
    ollama = _by_name(_collect(monkeypatch, runner=_RunnerStub(rc=0), urlopen=urlopen))[
        "ollama"
    ]
    assert ollama["status"] == "ok"


def test_ollama_daemon_unreachable_reports_unknown_and_optional(monkeypatch):
    urlopen = _UrlopenStub(exc=OSError(1, "connection refused"))
    checks = _collect(monkeypatch, runner=_RunnerStub(rc=0), urlopen=urlopen)
    ollama = _by_name(checks)["ollama"]
    assert ollama["status"] == "unknown"
    assert ollama["required"] is False
    _patch_collect(monkeypatch, checks)
    assert ic.main([]) == 0


def test_ollama_absent_is_optional_missing_and_main_returns_zero(monkeypatch):
    runner = _RunnerStub(rc=0)
    urlopen = _UrlopenStub(status=200)
    checks = _collect(
        monkeypatch,
        present=set(_ALL_TOOLS) - {"ollama"},
        runner=runner,
        urlopen=urlopen,
    )
    ollama = _by_name(checks)["ollama"]
    assert ollama["required"] is False
    assert ollama["status"] == "missing"
    assert urlopen.calls == [], "an absent binary must not be probed"
    _patch_collect(monkeypatch, checks)
    assert ic.main([]) == 0
    assert ic.main(["--json"]) == 0


# ---------------------------------------------------------------------------
# negative: probes never raise and never hang
# ---------------------------------------------------------------------------


def test_auth_probe_raising_oserror_reports_status_without_raising(monkeypatch):
    runner = _RunnerStub(exc=OSError(1, "spawn boom"))
    by_name = _by_name(_collect(monkeypatch, runner=runner))
    assert by_name["gh"]["status"] == "unknown"
    assert by_name["claude"]["status"] == "unknown"
    urlopen = _UrlopenStub(exc=OSError(1, "connection refused"))
    ollama = _by_name(
        _collect(monkeypatch, runner=_RunnerStub(rc=0), urlopen=urlopen)
    )["ollama"]
    assert ollama["status"] == "unknown"


def test_auth_probe_timeout_reports_status_and_passes_timeout(monkeypatch):
    runner = _RunnerStub(
        exc=subprocess.TimeoutExpired(cmd="gh", timeout=ic._PROBE_TIMEOUT_SECONDS)
    )
    by_name = _by_name(_collect(monkeypatch, runner=runner))
    assert by_name["gh"]["status"] == "unknown"
    assert by_name["claude"]["status"] == "unknown"
    assert runner.calls, "the auth probe never reached the runner"
    for call in runner.calls:
        assert call["timeout"] == ic._PROBE_TIMEOUT_SECONDS


def test_probe_timeout_constant_is_bounded():
    assert 0 < ic._PROBE_TIMEOUT_SECONDS <= 60


# ---------------------------------------------------------------------------
# negative/security: hints stay static, path-free, probe-output-free
# ---------------------------------------------------------------------------


def _current_username():
    try:
        return getpass.getuser()
    except (KeyError, OSError, ImportError):  # pragma: no cover - exotic hosts
        return None


def test_hints_stay_static_path_free_and_probe_output_free(monkeypatch):
    username = _current_username()
    poison_stdout = (
        f"user={username} token=secret /tmp/probe-out {os.environ.get('HOME', '')}"
    )
    poison_stderr = f"{_REPO_ROOT} {tempfile.gettempdir()} {os.getcwd()} stderr-secret"
    configurations = [
        # authorized everywhere
        (
            set(_ALL_TOOLS),
            _RunnerStub(rc=0, stdout=poison_stdout, stderr=poison_stderr),
            _UrlopenStub(200),
        ),
        # unauthorized everywhere
        (
            set(_ALL_TOOLS),
            _RunnerStub(rc=1, stdout=poison_stdout, stderr=poison_stderr),
            _UrlopenStub(401),
        ),
        # probes error out
        (
            set(_ALL_TOOLS),
            _RunnerStub(exc=OSError(1, "boom")),
            _UrlopenStub(exc=OSError(1, "refused")),
        ),
        # nothing installed at all
        (set(), _RunnerStub(rc=0), _UrlopenStub(200)),
    ]
    forbidden = [
        _STUB_PATH_PREFIX,
        str(_REPO_ROOT),
        os.path.expanduser("~"),
        tempfile.gettempdir(),
        os.getcwd(),
        os.environ.get("HOME", ""),
        poison_stdout,
        poison_stderr,
    ]
    if username:
        forbidden.append(username)
    for present, runner, urlopen in configurations:
        for check in _collect(
            monkeypatch, present=present, runner=runner, urlopen=urlopen
        ):
            hint = check["hint"]
            assert hint.strip(), check
            for literal in forbidden:
                if literal:
                    assert literal not in hint, (check, literal)
            # The new auth-remedy hints must stay free of path separators
            # (the pre-existing install hints name https:// URLs, which are
            # not paths and predate this story).
            if check["status"] in ("unauthorized", "unknown"):
                assert "/" not in hint, (check, "path separator in an auth hint")
            if check["name"] in present:
                assert _STUB_PATH_PREFIX in check["detail"], check


# ---------------------------------------------------------------------------
# seam default and main() boundary
# ---------------------------------------------------------------------------


def test_runner_seam_absent_keeps_presence_only_contract(monkeypatch):
    by_name = _by_name(_collect(monkeypatch, runner=None, urlopen=None))
    for name in _PROBED_TOOLS:
        assert by_name[name]["status"] == "ok", name


def test_main_returns_zero_when_required_tools_missing_and_unauthorized(
    monkeypatch, capsys
):
    required = ("python", "git", "gh", "claude")
    checks = [
        {
            "name": name,
            "required": name in required,
            "status": "unauthorized" if name in ("gh", "claude") else "missing",
            "detail": "not signed in" if name in ("gh", "claude") else "not found",
            "hint": f"Install {name} to continue",
        }
        for name in _ALL_TOOLS
    ]
    _patch_collect(monkeypatch, checks)
    assert ic.main([]) == 0
    capsys.readouterr()  # discard the text-mode output before the JSON run
    assert ic.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out) == checks


def test_main_renders_unauthorized_distinctly_and_returns_zero(monkeypatch, capsys):
    checks = [
        {
            "name": "gh",
            "required": True,
            "status": "unauthorized",
            "detail": "not signed in",
            "hint": "Authenticate the GitHub CLI: run: gh auth login",
        }
    ]
    _patch_collect(monkeypatch, checks)
    assert ic.main([]) == 0
    text_out = capsys.readouterr().out
    assert "[AUTH]" in text_out
    assert "gh auth login" in text_out
    assert ic.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out) == checks