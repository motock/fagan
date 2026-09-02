"""Pre-venv prerequisite doctor: report the tools this repo's pipeline needs.

Runs on a bare system python3 (before any venv exists), so this module
imports stdlib only - no pipeline/, app/, or third-party imports. It is
imported by tests/unit/test_install_checks.py and executed standalone via
``python3 scripts/install_checks.py [--json]``; scripts/install.sh (a later
story) will consume its report and decide what to do, so ``main`` always
returns 0: missing OPTIONAL tools are graceful degradation, not failure.

Checks, in report order: python >= 3.10, git, gh, claude (required);
ollama, docker (optional). Every check carries a static, actionable,
host-independent hint; a discovered binary path goes in ``detail``, never
in ``hint``.
"""

import argparse
import json
import shutil
import sys

_PY_MIN = (3, 10)
_PYTHON_HINT = "Install Python 3.10+ (the mcp SDK needs it)"

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


def collect_checks(which=shutil.which, py_version=None):
    """Return one status dict per check; ``which``/``py_version`` injectable.

    ``py_version`` is a (major, minor) tuple defaulting to the running
    interpreter's, resolved inside the body so tests can pass synthetic
    versions. Every tool lookup goes through the ``which`` parameter (never
    ``shutil.which`` directly) so stubs fully control the environment and no
    result is cached between calls.
    """
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
            f"Python {py_version[0]}.{py_version[1]} found, 3.10+ required"
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
    for name, required, hint in _TOOL_SPECS:
        found = which(name)
        status = "ok" if found else "missing"
        checks.append(
            {
                "name": name,
                "required": required,
                "status": status,
                "detail": found if found else "not found",
                "hint": hint,
            }
        )
    return checks


def main(argv=None):
    """Print one line per check (or a JSON list with --json); return 0."""
    parser = argparse.ArgumentParser(
        description="Report prerequisite tools for this repo (advisory only)."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the check results as a JSON list instead of text",
    )
    args = parser.parse_args(argv)
    checks = collect_checks()
    if args.json:
        print(json.dumps(checks, indent=2))
        return 0
    for check in checks:
        if check["status"] == "ok":
            marker = "ok"
        elif check["required"]:
            marker = "MISS"
        else:
            marker = "optional"
        print(f"[{marker}] {check['name']}: {check['detail']} - {check['hint']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())