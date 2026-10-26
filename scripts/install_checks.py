"""Pre-venv prerequisite doctor: report the tools this repo's pipeline needs.

Runs on a bare system python3 (before any venv exists), so this module
imports stdlib only - no pipeline/, app/, or third-party imports. It is
imported by tests/unit/test_install_checks.py and executed standalone via
``python3 scripts/install_checks.py [--json]``; scripts/install.sh (a later
story) will consume its report and decide what to do, so ``main`` always
returns 0: missing OPTIONAL tools are graceful degradation, not failure.

Checks, in report order: python >= 3.11, git, gh, claude (required);
ollama, docker (optional). Every check carries a static, actionable,
host-independent hint; a discovered binary path goes in ``detail``, never
in ``hint``.

Beyond presence, three authorizations are probed (PP-04): gh (the pipeline
opens and merges PRs through it, so an unauthenticated gh fails at the
first push), the Claude Code CLI (dispatch/review shell out to it), and
the local ollama daemon's ollama.com sign-in (a ``:cloud``-tagged model is
proxied through https://ollama.com by the daemon, which sends its own
credential - the app's OllamaProvider sends none). Statuses: ``ok``,
``missing``, ``unauthorized`` (present but not signed in - the remedy is a
login, not an install), and ``unknown`` (the probe could not decide). The
ollama sign-in is optional: only ``:cloud`` tags need it, so it never
fails the run.

The auth probes are reachable only through the two module-level seams
``_RUNNER`` (subprocess) and ``_URLOPEN`` (daemon HTTP), both ``None`` by
default: with the defaults, ``collect_checks`` stays presence-only - the
contract the base suite pins - while ``main`` installs the real
implementations so the standalone doctor verifies authorization too. Every
probe is bounded by ``_PROBE_TIMEOUT_SECONDS`` and never raises: a
timeout, a nonzero exit, a missing binary, or an OSError all resolve to an
``unknown``/``unauthorized`` status, never a crash and never a hang. The
probe helpers are nested inside ``collect_checks`` so this module keeps
exactly two top-level functions (a base-suite guarantee).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

_PY_MIN = (3, 11)
_PYTHON_HINT = "Install Python 3.11+ (the mcp SDK needs it)"

# The CLI each forge drives, and the env var that selects the forge. The
# doctor requires exactly the resolved forge's CLI - never both: a GitLab
# repo has no use for gh, and a GitHub repo has no use for glab.
_FORGE_ENV = "PIPELINE_FORGE"
_FORGE_CLI = {"github": "gh", "gitlab": "glab"}
_FORGE_HINTS = {
    "github": (
        "Install the GitHub CLI: https://cli.github.com ; then run: gh auth login"
    ),
    "gitlab": (
        "Install the GitLab CLI: https://gitlab.com/gitlab-org/cli ; "
        "then run: glab auth login"
    ),
}

# Bound for every auth probe (CLI subprocess or daemon HTTP call). A probe
# that hangs must degrade to ``unknown`` within this many seconds, never
# stall the doctor.
_PROBE_TIMEOUT_SECONDS = 10

# (name, required, hint) for the non-python checks, in report order. Hints
# are static strings so no host path, env value, or username can leak in.
_TOOL_SPECS = (
    ("git", True, "Install the git VCS from https://git-scm.com/downloads"),
    (
        "gh",
        True,
        "Install the GitHub CLI: https://cli.github.com ; then run: gh auth login",
    ),
    (
        "claude",
        True,
        "Install the Claude Code CLI: npm install -g @anthropic-ai/claude-code",
    ),
    (
        "ollama",
        False,
        "Optional: install ollama from https://ollama.com for local models",
    ),
    (
        "docker",
        False,
        "Optional: install Docker from https://docs.docker.com/get-docker/",
    ),
)

# Static, status-specific hint tables. A hint never embeds probe output, a
# host path, or an env value; the remedy it names is the only variable.
_UNAUTHORIZED_HINTS = {
    "gh": "Authenticate the GitHub CLI: run: gh auth login",
    "claude": "Authenticate the Claude Code CLI: run: claude auth login",
    "ollama": (
        "Optional: sign in to ollama.com for :cloud models: run: ollama signin"
    ),
}
_UNKNOWN_HINTS = {
    "gh": "Could not verify GitHub CLI auth; check it with: gh auth status",
    "claude": (
        "Could not verify Claude Code CLI auth; check it with: claude auth status"
    ),
    "ollama": (
        "Optional: could not reach the ollama daemon to check ollama.com "
        "sign-in; start it with: ollama serve"
    ),
}

# Resolved non-interactive probe surfaces (verified against the installed
# CLIs, not guessed): gh reports auth state via its exit code; the Claude
# Code CLI exposes ``auth status --json`` with a ``loggedIn`` boolean.
_GH_AUTH_ARGV = ("gh", "auth", "status")
_CLAUDE_AUTH_ARGV = ("claude", "auth", "status", "--json")
_OLLAMA_ME_URL = "http://127.0.0.1:11434/api/me"

# Injectable seams, both None by default so collect_checks stays
# presence-only (the base suite's pinned contract) and no test touches the
# live host. main() installs the real implementations below.
_RUNNER = None
_URLOPEN = None


def collect_checks(which=shutil.which, py_version=None):
    """Return one status dict per check; ``which``/``py_version`` injectable.

    ``py_version`` is a (major, minor) tuple defaulting to the running
    interpreter's, resolved inside the body so tests can pass synthetic
    versions. Every tool lookup goes through the ``which`` parameter (never
    ``shutil.which`` directly) so stubs fully control the suite's
    environment and no result is cached between calls.

    Authorization is probed only through the module-level ``_RUNNER`` and
    ``_URLOPEN`` seams; when they are unset (the default) the checks stay
    presence-only. A probe never raises and never hangs: any failure
    resolves to ``unknown``. The probe helpers are nested here so this
    module keeps exactly two top-level functions (a base-suite guarantee).
    """

    def cli_auth_status(argv, runner):
        """Probe a CLI's auth state; never raise, never hang.

        ``runner(argv, timeout) -> (returncode, stdout, stderr)``. Returns
        the raw result tuple so the caller can weigh the payload, or the
        string "unknown" when the probe could not run at all (a timeout, a
        missing binary, or any OSError is not proof of a signed-out state).
        """
        if runner is None:
            return None
        try:
            return runner(argv, timeout=_PROBE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return "unknown"
        except OSError:
            return "unknown"

    def ollama_signed_in(urlopen):
        """Probe the local daemon's ollama.com sign-in; never raise.

        POST /api/me returns 200 with the signed-in identity and 401 (or
        anything else) when not signed in; an unreachable daemon is
        unknown. urllib raises URLError (an OSError subclass) and
        TimeoutExpired (an OSError subclass since 3.10) - both degrade to
        unknown here.
        """
        if urlopen is None:
            return None
        request = urllib.request.Request(_OLLAMA_ME_URL, method="POST", data=b"{}")
        try:
            with urlopen(request, timeout=_PROBE_TIMEOUT_SECONDS) as response:
                return "ok" if response.status == 200 else "unauthorized"
        except OSError:
            return "unknown"

    def probe_auth(name):
        """Return (status, detail) for ``name``'s authorization, or None.

        None means "no probe configured" (seams unset): the check stays
        presence-only. The status vocabulary is ok/unauthorized/unknown and
        every failure mode degrades to unknown. ``detail`` stays short and
        static; the remedy lives in the hint tables at module level.
        """
        if name == "gh":
            result = cli_auth_status(_GH_AUTH_ARGV, _RUNNER)
            if result is None:
                return None
            if result == "unknown":
                return "unknown", "auth probe failed"
            # gh's exit code IS its auth verdict: 0 signed in, nonzero not.
            return ("ok", "") if result[0] == 0 else ("unauthorized", "not signed in")
        if name == "claude":
            result = cli_auth_status(_CLAUDE_AUTH_ARGV, _RUNNER)
            if result is None:
                return None
            if result == "unknown":
                return "unknown", "auth probe failed"
            # ``claude auth status --json`` prints {"loggedIn": bool}; the
            # payload is the verdict, not the exit code (a CLI without the
            # subcommand exits nonzero with no JSON, which is not proof of
            # a signed-out state => unknown).
            lowered = result[1].lower()
            if '"loggedin": true' in lowered:
                return "ok", ""
            if '"loggedin": false' in lowered:
                return "unauthorized", "not signed in"
            return "unknown", "auth probe failed"
        if name == "ollama":
            result = ollama_signed_in(_URLOPEN)
            if result is None:
                return None
            if result == "ok":
                return "ok", ""
            if result == "unauthorized":
                return "unauthorized", "daemon not signed in to ollama.com"
            return "unknown", "auth probe failed"
        return None

    def resolve_forge():
        """The active forge name: ``PIPELINE_FORGE``, else the origin host.

        Mirrors ``pipeline.forge.resolve_forge_name`` without importing it:
        this module must stay stdlib-only so it runs on a bare interpreter
        before any venv exists. An explicit ``github``/``gitlab`` wins and
        never pays for a remote probe; ``auto`` maps the origin remote's
        hostname and falls back to ``github`` when it is unrecognised. An
        unknown value is a misconfiguration, so it raises rather than
        silently picking a forge.
        """
        choice = os.environ.get(_FORGE_ENV, "auto").strip().lower()
        if choice in _FORGE_CLI:
            return choice
        if choice != "auto":
            raise ValueError(
                f"Unknown {_FORGE_ENV}={choice!r}; expected one of 'auto', "
                f"{sorted(_FORGE_CLI)}."
            )
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            proc = subprocess.run(
                ["git", "-C", repo_root, "remote", "get-url", "origin"],
                check=False, capture_output=True, text=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "github"
        url = (proc.stdout or "").strip() if proc.returncode == 0 else ""
        host = url.rsplit("@", 1)[-1].split(":")[0].split("/")[0].lower()
        return "gitlab" if "gitlab" in host else "github"

    # The forge's own CLI replaces the hardcoded gh entry, so a GitLab repo
    # requires glab and a GitHub repo requires gh - never both.
    forge = resolve_forge()
    forge_cli = _FORGE_CLI[forge]
    tool_specs = tuple(
        (forge_cli, required, _FORGE_HINTS[forge]) if name == "gh" else (name, required, hint)
        for name, required, hint in _TOOL_SPECS
    )

    if py_version is None:
        py_version = (sys.version_info.major, sys.version_info.minor)
    checks = []
    # The python check needs BOTH a locatable interpreter binary and a
    # sufficient version: with an empty PATH no binary is found, so the
    # check reports missing even when the running version is new enough.
    py_bin = which("python") or which("python3")
    if py_bin and py_version >= _PY_MIN:
        py_status = "ok"
        py_detail = f"Python {py_version[0]}.{py_version[1]} at {py_bin}"
    elif py_bin:
        py_status = "missing"
        py_detail = (
            f"Python {py_version[0]}.{py_version[1]} found, 3.11+ required"
        )
    else:
        py_status = "missing"
        py_detail = "not found"
    checks.append(
        {
            "name": "python",
            "required": True,
            "status": py_status,
            "detail": py_detail,
            "hint": _PYTHON_HINT,
        }
    )
    for name, required, hint in tool_specs:
        found = which(name)
        status = "ok" if found else "missing"
        detail = found if found else "not found"
        if found:
            auth = probe_auth(name)
            if auth is not None:
                status, auth_detail = auth
                if auth_detail:
                    detail = f"{found} - {auth_detail}"
                # The remedy for a present-but-unauthorized (or unverifiable)
                # tool is a login, not an install, so the hint is swapped for
                # the static status-specific one - in both text and JSON mode.
                if status == "unauthorized":
                    hint = _UNAUTHORIZED_HINTS.get(name, hint)
                elif status == "unknown":
                    hint = _UNKNOWN_HINTS.get(name, hint)
        checks.append(
            {
                "name": name,
                "required": required,
                "status": status,
                "detail": detail,
                "hint": hint,
            }
        )
    return checks


def main(argv=None):
    """Print one line per check (or a JSON list with --json); return 0.

    Installs the real subprocess/urllib probe seams before collecting, so
    the standalone doctor verifies authorization, not just presence. The
    exit code is always 0: a missing or unauthorized OPTIONAL tool is
    graceful degradation, not failure.
    """

    def install_real_seams():
        """Point the module seams at the real subprocess/urllib wrappers."""
        global _RUNNER, _URLOPEN
        _RUNNER = subprocess_runner
        _URLOPEN = urllib_urlopen

    def subprocess_runner(argv, timeout):
        """Default CLI runner: run argv, return (returncode, stdout, stderr)."""
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def urllib_urlopen(request, timeout=None):
        """Default daemon HTTP seam: a thin urllib.request.urlopen alias."""
        return urllib.request.urlopen(request, timeout=timeout)

    install_real_seams()
    parser = argparse.ArgumentParser(
        description="Report prerequisite tools for this repo (advisory only)."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the check results as a JSON list instead of text",
    )
    args = parser.parse_args(argv)
    try:
        checks = collect_checks()
    except ValueError as exc:
        # An unknown PIPELINE_FORGE is a misconfiguration, not a crash: the
        # doctor names it and exits non-zero instead of dumping a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 2

    def missing_required_cli():
        """The resolved forge's CLI when it is absent, else None.

        A missing forge CLI is not graceful degradation: the pipeline cannot
        open or merge a PR without it, so the doctor names both the resolved
        forge and the CLI it requires and exits non-zero. Only an explicitly
        configured forge fails the run - under ``auto`` the doctor stays a
        report (the base contract) because the host may simply not be the
        pipeline's.
        """
        configured = os.environ.get(_FORGE_ENV, "auto").strip().lower()
        if configured not in _FORGE_CLI:
            return None
        required = _FORGE_CLI[configured]
        for check in checks:
            if check["name"] == required and check["status"] == "missing":
                return configured, required
        return None

    missing = missing_required_cli()
    if args.json:
        print(json.dumps(checks, indent=2))
    else:
        for check in checks:
            if check["status"] == "ok":
                marker = "ok"
            elif check["status"] == "unauthorized":
                marker = "AUTH"
            elif check["status"] == "unknown":
                marker = "UNKNOWN"
            elif check["required"]:
                marker = "MISS"
            else:
                marker = "optional"
            print(f"[{marker}] {check['name']}: {check['detail']} - {check['hint']}")
    if missing is not None:
        forge, cli = missing
        print(
            f"error: resolved forge {forge!r} requires the {cli!r} CLI, "
            f"which was not found on PATH",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())