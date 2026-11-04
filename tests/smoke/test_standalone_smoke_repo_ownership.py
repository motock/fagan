"""The smoke's bare repository must be owned by the container's git user.

The pipeline container runs git as uid 10001. ``deploy/standalone/smoke.sh``
seeds the bare repository as the CI runner and bind-mounts it, and git refuses a
repository owned by another user ("dubious ownership"), whatever its mode bits.
``save_plan`` then fails with ``git clone failed for file:///git-origin/repo.git``
and the container log only says "git command failed".

These are static text checks on the script: no Docker daemon, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE = REPO_ROOT / "deploy" / "standalone" / "smoke.sh"

CONTAINER_GIT_UID = "10001"
CHOWN = f'chown -R {CONTAINER_GIT_UID}:{CONTAINER_GIT_UID} "$TMP/repo.git"'


def smoke_text() -> str:
    return SMOKE.read_text(encoding="utf-8")


def seed_repository_body() -> str:
    match = re.search(
        r"^seed_repository\(\) \{\n(.*?)^\}", smoke_text(), re.MULTILINE | re.DOTALL
    )
    assert match, "seed_repository() not found in smoke.sh"
    return match.group(1)


def test_seed_repository_hands_the_repo_to_the_container_git_user() -> None:
    assert CHOWN in seed_repository_body()


def test_the_chown_is_escalated_only_when_sudo_is_set() -> None:
    # macOS leaves SUDO empty (Docker Desktop maps ownership) and an unattended
    # local run must not prompt for sudo.
    chown_lines = [
        line for line in seed_repository_body().splitlines() if CHOWN in line
    ]

    assert chown_lines, "no chown line in seed_repository()"
    assert chown_lines[0].lstrip().startswith('[ -z "$SUDO" ] || $SUDO chown')


def test_the_chown_runs_after_the_seed_push() -> None:
    # Once the repo belongs to uid 10001 the runner must not push to it, so the
    # hand-over has to be the last step.
    body = seed_repository_body()

    assert body.index("push") < body.index(CHOWN)


def test_the_chown_runs_after_the_world_writable_chmod() -> None:
    body = seed_repository_body()

    assert body.index("chmod -R a+rwX") < body.index(CHOWN)


def test_sudo_is_decided_before_the_repository_is_seeded() -> None:
    text = smoke_text()
    seed_call = re.search(r"^seed_repository$", text, re.MULTILINE)
    sudo_assignment = text.index('SUDO="sudo"')

    assert seed_call, "seed_repository is never called at top level"
    assert sudo_assignment < seed_call.start()
