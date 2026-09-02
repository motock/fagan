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