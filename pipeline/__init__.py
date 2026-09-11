"""Fagan - autonomous SDLC agent pipeline package.

Deliberately does NOT re-export server's public surface here (e.g. via
`from .server import *`): server.py defines an MCP tool function named
`checkpoint`, which collides with the `pipeline.checkpoint` submodule -
a star-import would shadow the submodule attribute with that function,
breaking `from pipeline import checkpoint`. Backward compatibility for
`import pipeline_mcp_server as p` is handled by the separate top-level
app/pipeline_mcp_server.py shim, which imports directly from `pipeline.server`
and does not go through this package's namespace.
"""
from __future__ import annotations

import os
import sys


def _load_env_file(repo_root: str | os.PathLike[str], environ: dict[str, str]) -> None:
    """Populate ``environ`` from ``<repo_root>/.pipeline.env`` (CFG-E2).

    Extracted from the import-time bootstrap below so tests can drive the
    loader directly (the import side effect is inert under pytest). At import
    time it is called with the real ``os.environ`` so that every entrypoint —
    uvicorn app.dashboard, launchd ``python -m pipeline.scheduler_daemon``,
    the MCP server, ad-hoc scripts — resolves identical config regardless of
    how the process was started.

    - Opt-out: ``PIPELINE_SKIP_ENV_FILE=1`` in the REAL process environment
      makes this a no-op (the ``environ`` argument is only the write target).
      Skip wins over the override below: skip means skip.
    - Path override: ``PIPELINE_ENV_FILE`` in the ``environ`` argument names an
      explicit env file to load INSTEAD of ``<repo_root>/.pipeline.env``. This
      lets an operator keep the env file OUTSIDE the repo (launchd/standalone
      case) and lets tests point at per-test files instead of the shared repo
      root. Unset or empty means the default repo-root lookup. A named file
      that does not exist loads nothing: no fallback to the repo-root file,
      no exception.
    - Never raises: a missing, unreadable or malformed file leaves ``environ``
      completely untouched. The file is parsed in full before any key is
      copied, so a broken file cannot inject a partial set of keys.
    - No expansion: ``~`` and ``$VAR`` stay literal, matching the shell
      sourcing path; callers expand if they want to.
    """
    # Process-level opt-out: check the real os.environ, not the ``environ``
    # argument (that is the write target, not the source of the flag).
    if os.environ.get("PIPELINE_SKIP_ENV_FILE") == "1":
        return
    try:
        # Lazy import: keeps this module's own import side-effect free and
        # avoids any circular import (env_file imports nothing from pipeline).
        from pipeline import env_file

        # Explicit-path override (CFG-E2): read from the ``environ`` argument
        # (the mapping being populated), NOT os.environ, so callers and tests
        # can scope it. Unset/empty -> default repo-root lookup; a named file
        # that does not exist loads NOTHING (no fallback, no raise).
        override = environ.get("PIPELINE_ENV_FILE")
        if override:
            if not os.path.isfile(override):
                return
            env_path = override
        else:
            env_path = env_file.find_env_file(repo_root)
            if env_path is None:
                return
        # Parse the WHOLE file into a dict before touching environ: a
        # malformed or unreadable file must leave the environment completely
        # unchanged (no partial injection).
        parsed = env_file.parse_env_file(env_path)
        # PRECEDENCE: the file WINS over pre-existing environ values, matching
        # scripts/dashboard.sh DASHENV-1 (sourcing overwrites caller-exported
        # env — the file is durable operator intent). The shell path and the
        # Python path must agree, so this is a plain assignment, NOT
        # setdefault / "real env wins".
        for key, value in parsed.items():
            environ[key] = value
    except Exception:  # noqa: BLE001 - the import must never fail on env-file issues
        return


# Import-time bootstrap: pipeline/__init__.py runs before any pipeline
# submodule, so this is the only place that can populate os.environ before
# pipeline.paths / pipeline.config read it at their own import time. The repo
# root comes from __file__ (parent of the pipeline package), NOT cwd: launchd
# runs `python -m pipeline.scheduler_daemon` with cwd=/ and must still find
# <repo>/.pipeline.env.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Inert under pytest: a developer's real repo-root .pipeline.env (e.g. the
# PLAN_DIR written by CFG-D2's standalone-setup.sh) must never leak into a
# pytest run. The PIPELINE_SKIP_ENV_FILE opt-out is checked inside
# _load_env_file itself.
if "pytest" not in sys.modules:
    _load_env_file(_REPO_ROOT, os.environ)