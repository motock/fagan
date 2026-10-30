"""The image ships both start-up scripts root-owned and not writable.

``/app/deploy/entrypoint.sh`` is what PID 1 runs, and it sources
``/app/deploy/lib/operator_env.sh`` by the time it has exported
``PIPELINE_ANTHROPIC_API_KEY``, ``CLAUDE_CODE_OAUTH_TOKEN``, ``GH_TOKEN`` and
``OLLAMA_API_KEY`` into its own environment. The image runs as uid 10001, which
is also the uid agent children run as, and an agent is prompt-injectable code
whose Bash tool executes repository-controlled commands. A script owned by that
uid with the owner write bit set can be edited by such an agent and re-executed
on the next container start, with every mounted secret already exported - so the
ownership and mode of both files is a security property of the build, not
tidiness.

Mode arithmetic for the regression this guards: a process running as euid 10001
against a file at ``uid=10001 mode=0755`` is matched by the *owner* class, and
``0o755 & 0o200 == 0o200`` - write allowed. Root-owned ``0555`` leaves euid 10001
in the *other* class, where ``0o555 & 0o002 == 0`` - write denied, while root
keeps read and execute. Hardening the sourced library while leaving the caller
mutable does not close the channel, because the mutating uid is the uid the
caller runs as; both files have to leave the runtime uid's write reach.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "deploy" / "Dockerfile"

ENTRYPOINT = "./deploy/entrypoint.sh"
OPERATOR_ENV = "./deploy/lib/operator_env.sh"


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _copy_line(text: str, destination: str) -> str:
    """The single ``COPY`` instruction whose destination is *destination*."""
    matches = [
        line
        for line in text.splitlines()
        if line.startswith("COPY") and line.split()[-1] == destination
    ]
    assert len(matches) == 1, (
        f"expected exactly one COPY writing {destination}, found {matches!r}"
    )
    return matches[0]


def _chmod_modes(text: str) -> dict[str, str]:
    """Map every path a build-time ``chmod`` names to the mode it is given."""
    modes: dict[str, str] = {}
    for mode, path in re.findall(r"chmod\s+([0-7]{3,4})\s+(\S+)", text):
        modes[path] = mode
    return modes


def test_entrypoint_is_copied_root_owned() -> None:
    line = _copy_line(_dockerfile(), ENTRYPOINT)

    assert "--chown=0:0" in line, (
        "deploy/entrypoint.sh must be root-owned: it runs as PID 1 and sources "
        f"the credential loader, so its owner must not be the runtime uid, got {line!r}"
    )


def test_operator_env_is_copied_root_owned() -> None:
    line = _copy_line(_dockerfile(), OPERATOR_ENV)

    assert "--chown=0:0" in line, (
        f"deploy/lib/operator_env.sh must be root-owned, got {line!r}"
    )


def test_entrypoint_mode_removes_the_owner_write_bit() -> None:
    modes = _chmod_modes(_dockerfile())

    assert ENTRYPOINT in modes, (
        "the build must set an explicit mode on deploy/entrypoint.sh; the image "
        "runs it as uid 10001 and must not let that uid rewrite it"
    )
    mode = int(modes[ENTRYPOINT], 8)
    assert mode & 0o222 == 0, (
        f"deploy/entrypoint.sh must not be writable by its owner at build time "
        f"(owner is root, so 0555 is the expected mode), got {oct(mode)}"
    )


def test_operator_env_mode_is_not_writable() -> None:
    modes = _chmod_modes(_dockerfile())
    mode = int(modes[OPERATOR_ENV], 8)

    assert mode & 0o222 == 0, (
        f"deploy/lib/operator_env.sh must not be writable, got {oct(mode)}"
    )


def test_both_scripts_are_hardened_in_one_build_layer() -> None:
    # The two chmods belong to the same RUN: they are one ownership-hardening
    # step, and splitting them is how the caller got left mutable while the
    # callee was hardened.
    text = _dockerfile()
    for run in re.findall(r"^RUN (.*?)(?=\n[A-Z]|\Z)", text, re.MULTILINE | re.DOTALL):
        if ENTRYPOINT in run:
            assert OPERATOR_ENV in run, (
                "the entrypoint's mode must be set in the same RUN as the "
                "credential loader's; both scripts share one trust boundary"
            )
            return
    raise AssertionError("no build-time chmod sets the entrypoint's mode")
