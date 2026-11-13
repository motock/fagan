"""The single store-first read of a plan's manifest (ADR 0012 stage B).

Every caller that needs "the plan's manifest, from the database when there is
one, otherwise from this host's plan directory" goes through
:func:`read_plan_manifest`, and every caller that needs "which plans exist?"
goes through :func:`read_plan_names` - the same read one level up, from one
plan's manifest to the set of plans. There is exactly one implementation of
each on purpose: three hand-rolled copies previously drifted into two
*contradictory* failure contracts (one let a store error propagate, one
swallowed it), so a fix to one never reached the others and a reader could not
tell which was intended. A fourth copy, a plan listing in
``pipeline.tick_checks``, re-implemented the store probe behind
``_record_view`` for the same reason - a later fix to the store selection would
not have reached it - so the listing lives here too.

Two things are explicit rather than inherited:

``fallback_on_error``
    Whether a *failed* read degrades to the other source. It is a required
    keyword - there is no default, so every call site states its intent:

    * ``False`` - fail closed. A store error propagates instead of silently
      resolving from this host's file, which is what a caller that is about to
      *act* on the manifest wants (``pipeline.checkpoint._plan_manifest``,
      ``pipeline.worktree_patch.apply_patch``): reading the wrong copy of a
      plan would record a checkpoint against, or apply a diff to, a stale
      worktree. A plan neither source knows is still reported as ``None``.
    * ``True`` - never a gate. A store error is logged at debug level and the
      file is tried; a file that is missing, corrupt or not a JSON object
      yields ``None``. Used where the answer is advisory
      (``pipeline.persistence._plan_role_config``).

``plans_root``
    Optional. Omitted, the file fallback reads this host's plan root
    (``build_host_state().root()``), resolved on every call, so callers name no
    plans-directory binding of their own. An explicit path still wins. Either
    way the fallback path honours a patched binding, since the host root reads
    ``pipeline.server.PLAN_DIR`` live.

The legacy file store has no record view for this purpose. ``FileStore``'s
record view delegates straight back to the same JSON files, through a
``pipeline.server`` binding of its own - so consulting it adds no information,
and would make the developer's real plans directory (or a binding other than
the caller's) shadow the caller's fixture. With the file store - or any store
without a record view - the file read *is* the primary read. Only a genuinely
store-backed deployment (``PIPELINE_STORE=sqlite``) reads records first.

A manifest a source does yield is the answer: a store manifest without a
``role_config`` block means "no override", never "ask the file".
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .host_state import build_host_state
from .store_records import DEFAULT_NAMESPACE

logger = logging.getLogger(__name__)

__all__ = ["read_plan_manifest", "read_plan_names"]


def _host_plans_root(plans_root: Path | str | None) -> Path | str:
    """The caller's explicit root, else this host's plan root, resolved per call."""
    if plans_root is None:
        return build_host_state().root()
    return plans_root


def _record_view() -> Any | None:
    """The record view of the *currently selected* store, or ``None``.

    ``None`` means "read the file": either the store has no record view (an
    in-process stand-in), or it is the legacy :class:`pipeline.store.FileStore`,
    whose record view reads the very same JSON files through its own
    ``pipeline.server`` binding - see the module docstring.

    Both lookups are lazy (``pipeline.server`` is imported here, not at module
    load) so a test that patches ``pipeline.server._store`` is honoured.
    """
    from . import server
    from .store import FileStore

    store = getattr(server, "_store", None)
    if store is None or isinstance(store, FileStore):
        return None
    records = getattr(store, "records", None)
    if not callable(records):
        return None
    try:
        return records()
    except Exception:  # "no record view" is a valid answer
        logger.debug("store has no usable record view", exc_info=True)
        return None


def _read_manifest_file(
    plan_name: str, plans_root: Path | str, *, fallback_on_error: bool
) -> dict[str, Any] | None:
    """Read ``<plans_root>/<plan_name>.manifest.json``.

    Returns the parsed object, or ``None`` when there is no such plan: an
    absent file is *not found*, not a failure, so it always answers ``None``
    (callers report that as "unknown plan"). A file that exists but cannot be
    read or parsed is a failure: it propagates unless ``fallback_on_error`` is
    true, in which case it degrades to ``None``. Anything that parses to a
    non-object is ``None`` too - there is no manifest there.
    """
    path = Path(plans_root) / f"{plan_name}.manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        if not fallback_on_error:
            raise
        logger.debug("plan manifest unreadable at %s", path, exc_info=True)
        return None
    return manifest if isinstance(manifest, dict) else None


def read_plan_manifest(
    plan_name: str,
    *,
    fallback_on_error: bool,
    plans_root: Path | str | None = None,
) -> dict[str, Any] | None:
    """Read one plan's manifest, record view first, caller's plan file second.

    See the module docstring for the two explicit contracts
    (``fallback_on_error``, ``plans_root``) and for why the legacy file store
    reads the file as its primary path.

    The returned manifest is a read-only snapshot: callers only read it.
    """
    records = _record_view()
    if records is not None:
        try:
            manifest = records.get_manifest(DEFAULT_NAMESPACE, plan_name)
        except Exception:  # re-raised unless explicitly degraded
            if not fallback_on_error:
                raise
            logger.debug(
                "record store manifest read failed for plan %s",
                plan_name,
                exc_info=True,
            )
            manifest = None
        if isinstance(manifest, dict):
            return manifest
    return _read_manifest_file(
        plan_name, _host_plans_root(plans_root), fallback_on_error=fallback_on_error
    )


def _read_plan_names_file(
    plans_root: Path | str, *, fallback_on_error: bool
) -> list[str]:
    """Plan names present in ``plans_root``, sorted, from ``*.manifest.json``.

    An unreadable directory is a failure, so it propagates unless
    ``fallback_on_error`` is true, in which case it degrades to "no plans".
    """
    try:
        paths = sorted(Path(plans_root).glob("*.manifest.json"))
    except OSError:
        if not fallback_on_error:
            raise
        logger.debug("plan directory unreadable at %s", plans_root, exc_info=True)
        return []
    return [path.name.removesuffix(".manifest.json") for path in paths]


def read_plan_names(
    *, fallback_on_error: bool, plans_root: Path | str | None = None
) -> list[str]:
    """List the plan names this deployment knows, record view first.

    The listing counterpart of :func:`read_plan_manifest`, with the same two
    explicit contracts (``fallback_on_error``, ``plans_root`` - see the module
    docstring, including why the legacy file store's record view is not a second
    source). Every caller that needs "which plans exist?" goes through here, so
    a change to how the store is selected reaches all of them at once.

    A store that answers with a non-empty listing is the answer: the caller's
    plan directory is not consulted, so a store-backed deployment never grows
    phantom names from whatever manifests happen to sit on this host. An
    *empty* listing is treated as "no store view" rather than as a verdict that
    no plan exists, and falls through to the file - the alternative would make
    attribution depend on whether a plan happened to be recorded yet. A store
    error propagates unless ``fallback_on_error`` is true, in which case it is
    logged at debug level and the file is tried.

    Returns a sorted list of names, empty when neither source knows a plan.
    """
    records = _record_view()
    if records is not None:
        try:
            names = [str(name) for name in records.list_plans(DEFAULT_NAMESPACE)]
        except Exception:  # re-raised unless explicitly degraded
            if not fallback_on_error:
                raise
            logger.debug("record store plan listing failed", exc_info=True)
            names = []
        if names:
            return sorted(names)
    return _read_plan_names_file(
        _host_plans_root(plans_root), fallback_on_error=fallback_on_error
    )
