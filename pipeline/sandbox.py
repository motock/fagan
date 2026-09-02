"""PIPELINE_SANDBOX resolution for the pipeline's execution sandboxing.

Secure by Design: sandboxing ships OFF (``'none'``); operators opt in by
setting ``PIPELINE_SANDBOX``. Unknown values fail closed with ``ValueError``
rather than silently falling back to unsandboxed execution — mirroring how
``PIPELINE_EXEC_DISPATCH`` fails closed on unknown values and how
``app/backend.py``'s ``get_backend()`` raises on an unknown driver name.

Pure module: reads the environment at call time, imports only ``os``, and
depends on nothing else in the package.
"""

import os
import shutil

SANDBOX_ENV_VAR = "PIPELINE_SANDBOX"

_ALLOWED_SANDBOXES = ("none", "docker")


def resolve_sandbox() -> str:
    """Resolve the sandbox mode from the ``PIPELINE_SANDBOX`` env var.

    Returns ``'none'`` when the variable is unset, empty, or whitespace-only
    (the secure default), or one of the allowed values ``'none'``/``'docker'``
    (case-insensitive, surrounding whitespace tolerated). Any other value
    raises ``ValueError`` naming the variable, the offending value, and the
    allowed set — never a silent fallback for a typo'd value.
    """
    raw = os.environ.get(SANDBOX_ENV_VAR)
    if raw is None:
        return "none"
    stripped = raw.strip()
    if stripped == "":
        return "none"
    normalized = stripped.lower()
    if normalized == "none":
        return "none"
    if normalized == "docker":
        return "docker"
    raise ValueError(
        f"Invalid {SANDBOX_ENV_VAR} value {raw!r}: allowed values are "
        f"{list(_ALLOWED_SANDBOXES)}"
    )


def docker_binary_available() -> bool:
    """Return whether a ``docker`` binary is on the PATH.

    A pure probe: ``shutil.which('docker') is not None``. No side effects,
    no version negotiation; the lookup happens at call time, never cached
    at import time, so callers can re-probe after the environment changes.
    """
    return shutil.which("docker") is not None


def build_docker_command(
    worktree: str, argv: list[str], env: dict | None = None
) -> list[str]:
    """Build the ``docker run`` argv that wraps ``argv`` inside a container.

    Pure function: returns a new list and never mutates ``os.environ``, the
    caller's ``env``, or the caller's ``argv``.

    Shape::

        ['docker', 'run', '--rm', '-v', f'{worktree}:{worktree}',
         '--workdir', worktree, *env_flags, image, *argv]

    The worktree is volume-mounted AT ITS HOST PATH (identical path inside
    the container), so cwd-relative logic needs no remapping.

    Env passthrough is DENY-BY-DEFAULT: only keys whose name starts with
    ``'LOCAL_AGENT_'`` or ``'PIPELINE_'`` are forwarded, each as
    ``['-e', f'{key}={value}']``. Every other host env var is deliberately
    NOT forwarded (data minimization, Secure by Design). ``env=None`` means
    forward nothing — it never falls back to the host environment.

    The image comes from ``os.environ['PIPELINE_SANDBOX_IMAGE']``; when that
    var is unset or empty, raise ``ValueError`` naming the var — fail closed
    rather than guessing an image.
    """
    image = os.environ.get("PIPELINE_SANDBOX_IMAGE")
    if not image:
        raise ValueError(
            "PIPELINE_SANDBOX_IMAGE is not set: refusing to build a docker "
            "command without an image (set the variable to the container "
            "image to use)"
        )
    env_flags: list[str] = []
    if env is not None:
        for key, value in env.items():
            if key.startswith(("LOCAL_AGENT_", "PIPELINE_")):
                env_flags.extend(["-e", f"{key}={value}"])
    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{worktree}:{worktree}",
        "--workdir",
        worktree,
        *env_flags,
        image,
        *argv,
    ]
