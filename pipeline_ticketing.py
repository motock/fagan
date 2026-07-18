"""Ticketing backend abstraction for the pipeline MCP server.

Plane is one possible ticketing backend, not the only one, and the pipeline
already runs fully off its local manifest when no backend is configured.
TicketProvider makes that choice explicit and swappable via
PIPELINE_TICKET_PROVIDER instead of an implicit side effect of unset env
vars: "auto" (default) picks Plane if configured else no-op, "none" forces
the no-op provider even when Plane vars are present, "plane" forces Plane
(erroring if unconfigured), and "jira" is a documented extension point for a
future real implementation.

Every provider method mirrors the four operations the orchestrator actually
needs: create_epic, create_story, set_state (a transition, never raises),
and resolve_key (human key -> backend id). PlaneTicketProvider is a thin
delegate to the module-level Plane functions above rather than a
reimplementation, so it shares their caches, retry budget, and (in tests)
their exact plane_request call shape.

Tests patch the names in this module directly (monkeypatch.setattr(pt,
"plane_request", ...), monkeypatch.setattr(pt, "PLANE_API_KEY", ...), etc.)
- this is the Option B pattern from PIPELINE_MCP_DECOMPOSITION_PLAN.md §4:
the moved code reads its own module's globals, so the patch lands on the
binding the code actually reads. pipeline_mcp_server.py imports these names
for its own call sites (the @mcp.tool defs and the helpers that stay in the
server) but does NOT re-export them as the patch surface - tests patch this
module.
"""

import os
import re
from enum import Enum
from typing import Protocol

import httpx

from pipeline_config import PLANE_MAX_ATTEMPTS


# ---------- Plane config (env vars) ----------
PLANE_BASE      = os.environ.get("PLANE_BASE", "http://localhost").rstrip("/")
PLANE_API_KEY   = os.environ.get("PLANE_API_KEY", "")
PLANE_WORKSPACE = os.environ.get("PLANE_WORKSPACE", "")
PLANE_PROJECT   = os.environ.get("PLANE_PROJECT", "")


# ---------- Plane helpers ----------
def _plane_enabled() -> bool:
    """True only when Plane is fully configured (API key + workspace + project).

    When it isn't, the local manifest is the sole source of truth and every
    Plane call is skipped entirely rather than fired at an unconfigured
    endpoint. Without this guard an unconfigured deployment (the common case
    when running purely off manifests) 404s on every scheduled tick — burning
    the PLANE_MAX_ATTEMPTS retry budget and flooding the logs with dead
    requests to http://localhost/api/v1/workspaces//projects//...
    """
    return bool(PLANE_API_KEY and PLANE_WORKSPACE and PLANE_PROJECT)


def plane_request(method: str, path: str, **kwargs) -> dict:
    """Thin wrapper around Plane's REST API."""
    url = f"{PLANE_BASE}/api/v1/workspaces/{PLANE_WORKSPACE}{path}"
    headers = {"X-API-Key": PLANE_API_KEY, "Content-Type": "application/json"}
    r = httpx.request(method, url, headers=headers, timeout=30, **kwargs)
    if not r.is_success:
        raise RuntimeError(f"Plane {method} {path} → {r.status_code}: {r.text}")
    return r.json() if r.content else {}


_state_cache: dict[str, str] = {}


def _get_state(group: str) -> str:
    """Return the first state UUID matching the given Plane state group."""
    if group not in _state_cache:
        resp = plane_request("GET", f"/projects/{PLANE_PROJECT}/states/")
        for s in resp.get("results", []):
            _state_cache.setdefault(s["group"], s["id"])
    return _state_cache[group]


_label_cache: dict[str, str] = {}

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def _resolve_issue_uuid(story_key: str) -> str:
    """Return the Plane work-item UUID for story_key.

    Manifest keys are either plain UUIDs (plans created after ingest_plan was
    fixed) or human-readable identifiers like PIPE-7 (older plans / manual
    dispatch).  Plane's REST API only accepts the UUID form in URL paths, so
    we look up the UUID by sequence number when the key is not already a UUID.
    """
    if _UUID_RE.match(story_key):
        return story_key
    m = re.search(r'(\d+)$', story_key)
    if not m:
        return story_key  # can't parse — pass through and let Plane error
    seq = int(m.group(1))
    resp = plane_request(
        "GET",
        f"/projects/{PLANE_PROJECT}/work-items/",
        params={"sequence_id": seq},
    )
    results = resp.get("results", [])
    if results:
        return results[0]["id"]
    return story_key  # fallback — will produce a 404 from Plane


def _get_or_create_label(name: str) -> str:
    """Return the UUID for a label, creating it if it does not exist."""
    if name not in _label_cache:
        resp = plane_request("GET", f"/projects/{PLANE_PROJECT}/labels/")
        for lbl in resp.get("results", []):
            _label_cache[lbl["name"]] = lbl["id"]
    if name not in _label_cache:
        resp = plane_request("POST", f"/projects/{PLANE_PROJECT}/labels/",
                             json={"name": name, "color": "#6366f1"})
        _label_cache[name] = resp["id"]
    return _label_cache[name]


# ---------------------------------------------------------------------------
# TicketProvider abstraction
# ---------------------------------------------------------------------------
class LogicalState(Enum):
    """Backend-agnostic story lifecycle states, mapped to each provider's own
    vocabulary (e.g. Plane's state *groups*: backlog/started/completed)."""
    BACKLOG = "backlog"
    IN_PROGRESS = "in_progress"
    DONE = "done"


_PLANE_STATE_GROUP = {
    LogicalState.BACKLOG: "backlog",
    LogicalState.IN_PROGRESS: "started",
    LogicalState.DONE: "completed",
}


class TicketProvider(Protocol):
    """The seam between orchestration and whatever ticketing backend (or
    none) is configured. See the module comment above for the rationale."""

    enabled: bool

    def create_epic(self, summary: str) -> str | None:
        """Create an epic and return its backend id, or None if the backend
        has no epic concept / the call isn't supported (never an error)."""
        ...

    def create_story(
        self, summary: str, description: str, epic_id: str | None, label: str,
    ) -> str | None:
        """Create a story and return its backend id, or None if there is no
        backend to create it in (caller falls back to a local/synthetic key)."""
        ...

    def set_state(
        self, story_key: str, state: LogicalState, plan_name: str | None = None,
    ) -> bool:
        """Best-effort state transition. Never raises - returns whether it
        landed, mirroring _plane_set_state's contract."""
        ...

    def resolve_key(self, story_key: str) -> str:
        """Resolve a human-readable key (e.g. PIPE-7) to the backend's id."""
        ...


class NullTicketProvider:
    """No ticketing backend configured: every op is a no-op that reports
    success. The manifest remains the sole source of truth."""

    enabled = False

    def create_epic(self, summary: str) -> str | None:
        return None

    def create_story(
        self, summary: str, description: str, epic_id: str | None, label: str,
    ) -> str | None:
        return None

    def set_state(
        self, story_key: str, state: LogicalState, plan_name: str | None = None,
    ) -> bool:
        return True

    def resolve_key(self, story_key: str) -> str:
        return story_key


class PlaneTicketProvider:
    """Delegates to the existing module-level Plane functions unchanged, so
    it shares their caches (_state_cache/_label_cache) and their retry/
    never-raise contract (_plane_set_state) rather than reimplementing it."""

    @property
    def enabled(self) -> bool:
        return _plane_enabled()

    def create_epic(self, summary: str) -> str | None:
        # Epics are an optional Plane module; some instances/API versions do
        # not expose the /epics/ endpoint. Any failure here - a 404 from an
        # unsupported module just as much as a connection refused/timeout/
        # other transport error - must fall back to ungrouped issues rather
        # than propagate out of ingest_plan, matching create_story below.
        try:
            resp = plane_request("POST", f"/projects/{PLANE_PROJECT}/epics/",
                                  json={"name": summary})
            return resp["id"]
        except Exception as e:
            print(f"Warning: Plane create_epic({summary!r}) failed, "
                  f"continuing without a Plane epic: {e}")
            return None

    def create_story(
        self, summary: str, description: str, epic_id: str | None, label: str,
    ) -> str | None:
        # Mirrors create_epic: a ticketing outage (connection refused,
        # timeout, non-2xx response, ...) must not block ingest_plan, which
        # falls back to a local/synthetic story key when this returns None.
        try:
            label_id = _get_or_create_label(label)
            backlog_state = _get_state("backlog")
            issue_resp = plane_request("POST", f"/projects/{PLANE_PROJECT}/work-items/", json={
                "name": summary,
                "description": description,
                "state": backlog_state,
                "labels": [label_id],
            })
            issue_id = issue_resp["id"]
        except Exception as e:
            print(f"Warning: Plane create_story({summary!r}) failed, "
                  f"falling back to a local/synthetic story key: {e}")
            return None
        # The epic link is a second, separate call after a real issue has
        # already been created. Its failure must only degrade the link (the
        # issue stays ungrouped, like create_epic's own optional-module
        # fallback) - not discard the just-created issue_id, which would
        # orphan a real Plane ticket and cause a retried ingest_plan to
        # create a duplicate.
        if epic_id is not None:
            try:
                plane_request("POST", f"/projects/{PLANE_PROJECT}/epics/{epic_id}/issues/",
                              json={"issue_id": issue_id})
            except Exception as e:
                print(f"Warning: Plane create_story({summary!r}) succeeded but "
                      f"linking issue {issue_id!r} to epic {epic_id!r} failed, "
                      f"continuing without the epic link: {e}")
        return issue_id

    def set_state(
        self, story_key: str, state: LogicalState, plan_name: str | None = None,
    ) -> bool:
        return _plane_set_state(story_key, _PLANE_STATE_GROUP[state], plan_name)

    def resolve_key(self, story_key: str) -> str:
        return _resolve_issue_uuid(story_key)


class JiraTicketProvider:
    """Documented extension point, not a working implementation.

    A real Jira Cloud integration needs to handle three things this stub
    does not: transitions go through POST /issue/{key}/transitions (Jira has
    no single `state` PATCH like Plane's), auth is `Authorization: Basic
    base64(email:api_token)` (or OAuth bearer) rather than Plane's
    `X-API-Key`, and issue descriptions must be Atlassian Document Format
    (ADF), not plain text. See TICKETING_ABSTRACTION_PLAN.md S5.
    """

    enabled = True

    def _unimplemented(self) -> None:
        raise NotImplementedError(
            "JiraTicketProvider is a documented stub, not a working "
            "integration. See TICKETING_ABSTRACTION_PLAN.md S5 for what a "
            "real implementation must handle (transition IDs, Basic/Bearer "
            "auth, ADF description format)."
        )

    def create_epic(self, summary: str) -> str | None:
        self._unimplemented()

    def create_story(
        self, summary: str, description: str, epic_id: str | None, label: str,
    ) -> str | None:
        self._unimplemented()

    def set_state(
        self, story_key: str, state: LogicalState, plan_name: str | None = None,
    ) -> bool:
        self._unimplemented()

    def resolve_key(self, story_key: str) -> str:
        self._unimplemented()


_TICKET_PROVIDERS: dict[str, type] = {
    "plane": PlaneTicketProvider,
    "jira": JiraTicketProvider,
}


def get_ticket_provider() -> TicketProvider:
    """Resolve the active ticketing backend from PIPELINE_TICKET_PROVIDER.

    "auto" (default) preserves today's behavior exactly: Plane if configured,
    else the no-op provider. "none" forces the no-op provider even when Plane
    vars are set. "plane"/"jira" force that backend, erroring clearly if its
    required config is missing rather than silently falling back.
    """
    choice = os.environ.get("PIPELINE_TICKET_PROVIDER", "auto").strip().lower()
    if choice == "auto":
        return PlaneTicketProvider() if _plane_enabled() else NullTicketProvider()
    if choice == "none":
        return NullTicketProvider()
    if choice == "plane" and not _plane_enabled():
        raise ValueError(
            "PIPELINE_TICKET_PROVIDER=plane requires PLANE_API_KEY, "
            "PLANE_WORKSPACE, and PLANE_PROJECT to all be set."
        )
    provider_cls = _TICKET_PROVIDERS.get(choice)
    if provider_cls is None:
        raise ValueError(
            f"Unknown PIPELINE_TICKET_PROVIDER={choice!r}; expected one of "
            f"'auto', 'none', {sorted(_TICKET_PROVIDERS)}."
        )
    return provider_cls()


def _plane_set_state(story_key: str, state_group: str, plan_name: str | None = None) -> bool:
    """Best-effort Plane state transition with an inline retry budget.

    Plane sync is a side effect of an action that already succeeded in git, so
    it never raises: a transient failure is retried up to PLANE_MAX_ATTEMPTS,
    and once the budget is spent the drop is recorded durably (via _notify_user
    when a plan is known, else a stderr-style print) rather than propagated.
    Returns True if the transition landed, False if it was given up on.
    """
    if not _plane_enabled():
        return True  # no Plane to sync to; the manifest is the source of truth
    last_err: Exception | None = None
    for _ in range(max(1, PLANE_MAX_ATTEMPTS)):
        try:
            issue_uuid = _resolve_issue_uuid(story_key)
            plane_request("PATCH", f"/projects/{PLANE_PROJECT}/work-items/{issue_uuid}/",
                          json={"state": _get_state(state_group)})
            return True
        except Exception as e:
            last_err = e
    msg = (f"Plane sync for {story_key} → {state_group} failed after "
           f"{PLANE_MAX_ATTEMPTS} attempts: {last_err}")
    if plan_name:
        # Lazy import to avoid a circular dependency: pipeline_mcp_server
        # imports this module at top level, so importing it at module load
        # here would cycle. _notify_user lives in the server module because
        # it reads PLAN_DIR (a pipeline_paths constant re-exported by the
        # server, patched by tests via p.PLAN_DIR).
        from pipeline_mcp_server import _notify_user
        _notify_user(plan_name, msg)
    else:
        print(f"Warning: {msg}")
    return False


def _mark_plane_done(story_key: str, plan_name: str | None = None) -> None:
    """Best-effort transition of a story's ticket to Done, via whichever
    TicketProvider is configured (PIPELINE_TICKET_PROVIDER).

    Mirrors dispatch_story's in-progress transition: swallows errors rather
    than raising, since not every plan is ticket-backed and a ticketing
    outage must not block a local merge that has already happened in git.
    Named for the merge-path callers (advance_pipeline, approve_merge) that
    predate the TicketProvider abstraction; kept as the call site so their
    existing test mocks don't need to change.
    """
    get_ticket_provider().set_state(story_key, LogicalState.DONE, plan_name)


__all__ = [
    "PLANE_BASE",
    "PLANE_API_KEY",
    "PLANE_WORKSPACE",
    "PLANE_PROJECT",
    "_plane_enabled",
    "plane_request",
    "_state_cache",
    "_get_state",
    "_label_cache",
    "_UUID_RE",
    "_resolve_issue_uuid",
    "_get_or_create_label",
    "LogicalState",
    "_PLANE_STATE_GROUP",
    "TicketProvider",
    "NullTicketProvider",
    "PlaneTicketProvider",
    "JiraTicketProvider",
    "_TICKET_PROVIDERS",
    "get_ticket_provider",
    "_plane_set_state",
    "_mark_plane_done",
]