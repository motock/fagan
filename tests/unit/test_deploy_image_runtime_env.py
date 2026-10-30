"""Runtime-stage environment contract for the deployable server image.

The server image ships the persona directory and the virtualenv the local
dispatch harness expects, and it can commit on its own (the pipeline creates
WIP checkpoint commits inside the container).  Three pieces of the runtime
stage make that work:

* ``AGENTS_DIR=/app/agents`` -- ``pipeline/paths.py`` otherwise falls back to
  ``~/.claude/agents`` (``/var/lib/pipeline/.claude/agents``), where no persona
  exists, and story dispatch dies with ``FileNotFoundError``;
* ``/app/.venv`` -> ``/opt/venv`` -- ``app/backend_ollama.py`` launches
  ``<repo>/.venv/bin/python3``, which is ``/app/.venv/bin/python3`` in the
  image, while the image's virtualenv lives at ``/opt/venv``;
* ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` -- without an identity the checkpoint
  commit fails with ``Author identity unknown``.

These tests read ``deploy/Dockerfile`` as text, split it at the final ``FROM``
and assert every invariant against the *runtime* half only, so an instruction
accidentally added to the builder stage cannot satisfy them.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "deploy" / "Dockerfile"

GIT_IDENTITY_NAMES = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
)

ADDED_ENV_NAMES = ("AGENTS_DIR", *GIT_IDENTITY_NAMES)

SECRET_NAME_FRAGMENTS = ("TOKEN", "KEY", "SECRET", "PASSWORD")


def _read_dockerfile() -> str:
    assert DOCKERFILE.is_file(), "deploy/Dockerfile does not exist yet"
    return DOCKERFILE.read_text(encoding="utf-8")


def _split_stages(text: str) -> tuple[str, str, int]:
    """Return ``(builder, runtime, runtime_offset)``.

    ``runtime`` is everything from the final ``FROM`` instruction onwards; the
    offset is that instruction's index in ``text``.
    """
    froms = [match.start() for match in re.finditer(r"(?m)^FROM ", text)]
    assert froms, "deploy/Dockerfile declares no FROM instruction"
    last_from = froms[-1]
    return text[:last_from], text[last_from:], last_from


def test_runtime_stage_sets_agents_dir() -> None:
    _, runtime, _ = _split_stages(_read_dockerfile())

    assert re.search(r"(?m)^\s*AGENTS_DIR=/app/agents\s*\\?$", runtime), (
        "the runtime stage must set AGENTS_DIR=/app/agents"
    )
    # ...and that path must be where the personas are actually copied to.
    assert "COPY --chown=10001:10001 agents ./agents" in runtime
    assert re.search(r"(?m)^\s*WORKDIR /app\s*$", runtime), (
        "the runtime stage must set WORKDIR /app so ./agents lands at /app/agents"
    )


def test_runtime_stage_sets_git_identity() -> None:
    _, runtime, _ = _split_stages(_read_dockerfile())

    for name in GIT_IDENTITY_NAMES:
        match = re.search(r"(?m)^\s*" + name + r"=(\S+)", runtime)
        assert match is not None, f"the runtime stage must set {name}"
        assert match.group(1), f"{name} must have a non-empty value"


def test_runtime_stage_creates_venv_symlink() -> None:
    _, runtime, _ = _split_stages(_read_dockerfile())

    assert "/app/.venv" in runtime
    normalized = re.sub(r"\s+", " ", runtime)
    assert "ln -s /opt/venv /app/.venv" in normalized, (
        "the runtime stage must link /app/.venv to /opt/venv"
    )


def test_new_env_names_are_not_secret_looking() -> None:
    _, runtime, _ = _split_stages(_read_dockerfile())

    for name in ADDED_ENV_NAMES:
        assert re.search(r"(?m)^\s*" + name + r"=", runtime), (
            f"{name} must be declared in the runtime stage before its name can "
            "be audited for secret-looking defaults"
        )
        for fragment in SECRET_NAME_FRAGMENTS:
            assert fragment not in name, (
                f"{name} looks like a secret; credentials must arrive as mounted "
                "files at runtime, never as a build-time ENV default"
            )


def test_builder_stage_does_not_set_agents_dir() -> None:
    text = _read_dockerfile()
    builder, _, last_from = _split_stages(text)

    assert "AGENTS_DIR" not in builder, (
        "AGENTS_DIR is a runtime-stage setting; the builder stage only builds "
        "the virtualenv"
    )
    assert "AGENTS_DIR" in text, "the runtime stage must set AGENTS_DIR"
    assert last_from < text.index("AGENTS_DIR"), (
        "AGENTS_DIR must be declared after the final FROM (runtime stage)"
    )
