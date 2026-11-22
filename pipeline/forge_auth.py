"""The forge credential for the pipeline's own git and gh commands.

A dispatched agent gets its namespace's forge token through
``pipeline/execution.py::_namespace_forge_env``. The pipeline process runs its
own ``git push``, ``gh pr create``, merge and CI-status commands too, and those
need the same token in a team namespace. :func:`env_kw` builds the keyword
arguments that carry it, meant to be splatted into a ``subprocess.run`` call::

    subprocess.run([...], cwd=..., **forge_auth.env_kw())

The default namespace gets exactly the call it had before: no keyword at all,
or ``env=base`` when the caller already passed an environment. A team
namespace never runs on the process token. It gets its own token from the
credential seam, or none, so a team without a token fails at the remote.
"""

import os

from app.credentials import (
    CredentialMissing,
    CredentialRef,
    is_default_namespace,
    resolve_for_injection,
)
from pipeline.actor import current_namespace

# The process's own forge tokens. A team's commands never receive these.
_PROCESS_FORGE_TOKENS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)

# The first entry clears every credential helper the environment inherited,
# so only the gh helper below answers for github.com.
_GIT_CREDENTIAL_CONFIG = (
    ("credential.helper", ""),
    ("credential.https://github.com.helper", "!gh auth git-credential"),
)


def env_kw(base: dict | None = None) -> dict:
    """Return the ``subprocess.run`` keyword arguments for the bound namespace.

    ``base`` is the environment the call would otherwise get; ``None`` means
    the process environment. It is copied, never mutated.
    """
    namespace = current_namespace()
    if is_default_namespace(namespace):
        return {} if base is None else {"env": base}
    env = dict(os.environ if base is None else base)
    for name in _PROCESS_FORGE_TOKENS:
        env.pop(name, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    _append_git_config(env, _GIT_CREDENTIAL_CONFIG)
    try:
        env["GH_TOKEN"] = resolve_for_injection(
            CredentialRef("forge", "github_token", namespace=namespace)
        )
    except CredentialMissing:
        pass
    return {"env": env}


def _append_git_config(env: dict, entries: tuple[tuple[str, str], ...]) -> None:
    """Append ``entries`` after any ``GIT_CONFIG_*`` pairs ``env`` already has."""
    try:
        count = max(int(env.get("GIT_CONFIG_COUNT", "0")), 0)
    except ValueError:
        count = 0
    for key, value in entries:
        env[f"GIT_CONFIG_KEY_{count}"] = key
        env[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
