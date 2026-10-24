"""Render the decompose-phase external context for a selected workspace.

Decompose runs before a plan exists, so only the workspace's ``.fagan.json``
``context`` block applies (docs/plans/EXTERNAL_CONTEXT_SOURCES_PLAN.md §4.6).
"""

import logging
from pathlib import Path

from . import context_sources as ctxsrc
from .repo_config import RepoConfigError, load_repo_config

_log = logging.getLogger("pipeline")

DECOMPOSE_PHASE = "decompose"


def build_decompose_context_block(repo_root: str | Path) -> str:
    """Return the rendered decompose-phase block for ``repo_root`` ("" if none).

    Raises ContextSourceError (or RepoConfigError for an invalid
    ``.fagan.json``) when a required source cannot be used; an optional
    source that fails is skipped with a warning.
    """
    config = load_repo_config(Path(repo_root))
    context = (config or {}).get("context")
    if not context:
        return ""
    sources = ctxsrc.sources_for_phase(ctxsrc.normalize_context_sources(context), DECOMPOSE_PHASE)
    resolved = []
    for source in sources:
        try:
            loaded = ctxsrc.resolve_dir_source(source)
        except ctxsrc.ContextSourceError as exc:
            if source["required"]:
                raise ctxsrc.ContextSourceError(f"required context source failed: {exc}") from exc
            _log.warning(
                "optional decompose context source unavailable",
                extra={"source": source["name"]},
            )
            continue
        resolved.append({**source, **{k: loaded[k] for k in ("text", "digest", "bytes")}})
    max_total = context.get("max_total_chars", ctxsrc.DEFAULT_MAX_TOTAL_CHARS)
    return ctxsrc.render_context_block(resolved, max_total_chars=max_total)


__all__ = ["RepoConfigError", "build_decompose_context_block"]
