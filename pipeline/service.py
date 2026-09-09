"""Service seam for pipeline operations (W1a).

``PipelineService`` was extracted verbatim from ``pipeline/server.py``. Its
methods are thin delegators that call module-level ``_*_impl`` functions and
read module globals (``PLAN_DIR``, ``_store``, ``_VALID_STORY_STATUSES``, ...)
as free variables. Those names still live in ``pipeline.server`` (and will be
moved by LATER stories in this chain), so this module resolves them through
``_ServerRef`` bindings that delegate to the *current* ``pipeline.server``
namespace at call time.

This mirrors the ``_ServerRef`` pattern already used by ``pipeline/store.py``:
the test suite patches ``pipeline.server`` for these names (e.g.
``monkeypatch.setattr(pipeline.server, "_set_plan_paused", fake)``), so the
bindings must read the live ``pipeline.server`` value at call time rather than
hold a copy imported at module load (which would freeze the real values into
every test run and break the patch targets).
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Any

from pipeline.workspace import normalize_workspace_path, validate_workspace

__all__ = ["PipelineService"]


class _ServerRef:
    """Delegates to the *current* ``pipeline.server`` binding for a name.

    The class body below references server-sourced names (``_store``,
    ``PLAN_DIR``, ``_set_plan_paused``, ``_validate_key``, ...) as bare module
    globals. The test suite patches ``pipeline.server`` for those names, so
    these bindings must read the live ``pipeline.server`` value at call time
    rather than hold a copy imported at module load.
    """

    def __init__(self, name: str):
        self._name = name

    def _value(self):
        from . import server as _server
        return getattr(_server, self._name)

    def __getattr__(self, attr: str):
        return getattr(self._value(), attr)

    def __call__(self, *args, **kwargs):
        return self._value()(*args, **kwargs)

    def __truediv__(self, other):
        return self._value() / other

    def __contains__(self, item):
        return item in self._value()

    def __iter__(self):
        return iter(self._value())

    def __sub__(self, other):
        return self._value() - other

    def __rsub__(self, other):
        return other - self._value()

    def __len__(self):
        return len(self._value())

    def __eq__(self, other):
        if isinstance(other, _ServerRef):
            return self._value() == other._value()
        return self._value() == other

    def __lt__(self, other):
        return self._value() < other

    def __le__(self, other):
        return self._value() <= other

    def __gt__(self, other):
        return self._value() > other

    def __ge__(self, other):
        return self._value() >= other

    def __hash__(self):
        return hash(self._value())

    def __str__(self):
        return str(self._value())

    def __repr__(self):
        return repr(self._value())


# Server-sourced names the class body references as free variables. Each
# resolves to the live ``pipeline.server`` binding at call time so
# ``monkeypatch.setattr(pipeline.server, "NAME", ...)`` still lands.
_store = _ServerRef("_store")
PLAN_DIR = _ServerRef("PLAN_DIR")
config_provenance = _ServerRef("config_provenance")
role_registry = _ServerRef("role_registry")
_validate_key = _ServerRef("_validate_key")
_set_plan_paused = _ServerRef("_set_plan_paused")
_decisions_path = _ServerRef("_decisions_path")
_get_effective_config_impl = _ServerRef("_get_effective_config_impl")
_approve_merge_impl = _ServerRef("_approve_merge_impl")
_ingest_plan_impl = _ServerRef("_ingest_plan_impl")
_scoped_repo_root = _ServerRef("_scoped_repo_root")
_load_policy = _ServerRef("_load_policy")
_parse_ruling = _ServerRef("_parse_ruling")
_invoke_overlord = _ServerRef("_invoke_overlord")
_plan_role_config = _ServerRef("_plan_role_config")
get_ticket_provider = _ServerRef("get_ticket_provider")
LogicalState = _ServerRef("LogicalState")
_checkpoint_impl = _ServerRef("_checkpoint_impl")
_terminate_and_checkpoint = _ServerRef("_terminate_and_checkpoint")
_completed_dep_ids = _ServerRef("_completed_dep_ids")
_atomic_write_json = _ServerRef("_atomic_write_json")
advance_pipeline = _ServerRef("advance_pipeline")
_persona_default_model = _ServerRef("_persona_default_model")
DEFAULT_MODEL = _ServerRef("DEFAULT_MODEL")
_run_decompose = _ServerRef("_run_decompose")
_run_decompose_detailed = _ServerRef("_run_decompose_detailed")
_extract_json_block = _ServerRef("_extract_json_block")
_VALID_STORY_STATUSES = frozenset(
    (
        "todo",
        "in_progress",
        "running",
        "interrupted",
        "failed",
        "tests_passed",
        "pr_open",
        "changes_requested",
        "parked",
        "done",
        "done",
    )
)
_PATCHABLE_STORY_FIELDS = frozenset(
    (
        "agent_instructions",
        "model",
        "persona",
        "risk",
        "dependencies",
        "acceptance",
        "pr_url",
        "summary",
        "tdd_split",
        "backend",
    )
)
_VALID_STORY_BACKENDS = frozenset(
    {"claude", "local", "ollama", "lmstudio", "mlx", "litellm", "auto"}
)
_dispatch_story_impl = _ServerRef("_dispatch_story_impl")
_original_review_story = _ServerRef("_original_review_story")
_advance_pipeline_locked = _ServerRef("_advance_pipeline_locked")
_mark_story_done_impl = _ServerRef("_mark_story_done_impl")
_check_usage_impl = _ServerRef("_check_usage_impl")


class PipelineService:
    """Transport-agnostic pipeline operations.

    The ``@mcp.tool()`` functions below are one-line delegations to these
    methods, so a future HTTP adapter can drive the same logic without MCP
    (see docs/plans/PLATFORM_DECOUPLING_AND_SCALE_PLAN.md, W1a).

    Methods deliberately read this module's globals (``PLAN_DIR``,
    ``PIPELINE_AUTONOMY``, ``_plan_lock``, ...) as free variables rather than
    holding copies, so the test suite's ``monkeypatch.setattr(pipeline.server,
    ...)`` targets keep landing exactly as they did before the extraction.
    """

    def get_effective_config(self, plan_name: str | None = None) -> dict[str, Any]:
        return _get_effective_config_impl(plan_name)

    def set_role_default(
        self, role: str, provider: str | None, model: str | None
    ) -> dict[str, Any]:
        """Set a role's global default (provider, model) in model_registry.json.

        Security-sensitive: a bad edit retargets a pipeline role to an
        arbitrary model, so we validate against the registry BEFORE writing —
        a typo must never reach disk. Fails closed (returns ``{ok: False}``
        without touching the file) on any unknown role, provider, or model,
        and writes atomically only to the registry path.
        """
        if role not in config_provenance.PIPELINE_ROLES:
            return {"ok": False, "error": f"unknown role {role!r}"}
        if not provider or not model:
            return {
                "ok": False,
                "error": f"provider and model must be non-empty (got provider={provider!r}, model={model!r})",
            }

        registry = role_registry.load_registry()
        providers = registry.get("providers", {})
        if provider not in providers:
            return {
                "ok": False,
                "error": f"unknown provider {provider!r}: not declared under providers",
            }
        if model not in providers[provider].get("models", {}):
            return {
                "ok": False,
                "error": (
                    f"unknown model {model!r}: not declared under "
                    f"providers.{provider}.models"
                ),
            }

        registry.setdefault("roles", {})[role] = {"provider": provider, "model": model}

        path = role_registry._registry_path()
        tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
        try:
            tmp.write_text(json.dumps(registry, indent=2))
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        return {"ok": True, "role": role, "provider": provider, "model": model}

    def set_plan_role_config(
        self,
        plan_name: str,
        role: str,
        provider: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Set a per-plan role_config override (provider/model) for one role.

        Takes effect immediately (no restart) because get_effective_config /
        _plan_role_config read the manifest fresh on every call. Validates the
        NEW role_config against the registry BEFORE saving — a typo'd or
        undeclared model must never reach disk. Writes atomically via
        _store.transaction so it's atomic w.r.t. the scheduler's 60s tick.
        """
        _validate_key(plan_name)
        # Fail closed at the boundary BEFORE taking the lock or touching disk:
        # an unknown role, empty provider/model, or undeclared provider/model
        # must never reach the manifest. This mirrors set_role_default's
        # validation so the two write paths enforce the same contract.
        if role not in config_provenance.PIPELINE_ROLES:
            return {"ok": False, "error": f"unknown role {role!r}"}
        # None means "not specified" (a partial entry is allowed, matching the
        # overrides contract); an empty/whitespace STRING is an explicit bad
        # value that must be rejected before it reaches disk.
        if provider is not None and not str(provider).strip():
            return {
                "ok": False,
                "error": f"provider must be non-empty (got provider={provider!r})",
            }
        if model is not None and not str(model).strip():
            return {
                "ok": False,
                "error": f"model must be non-empty (got model={model!r})",
            }
        registry = role_registry.load_registry()
        providers = registry.get("providers", {})
        if provider is not None and provider not in providers:
            return {
                "ok": False,
                "error": f"unknown provider {provider!r}: not declared under providers",
            }
        # Only validate the model against a provider's declared models when a
        # provider is given; a model-only partial (provider=None) is resolved
        # and validated by resolve_role below against the effective provider.
        if (
            provider is not None
            and model is not None
            and model not in providers[provider].get("models", {})
        ):
            return {
                "ok": False,
                "error": (
                    f"unknown model {model!r}: not declared under "
                    f"providers.{provider}.models"
                ),
            }
        with _store.transaction(plan_name) as acquired:
            if not acquired:
                return {"ok": True, "skipped": "locked"}
            if not _store.manifest_path(plan_name).exists():
                return {"ok": False, "error": f"No such plan {plan_name!r}"}
            manifest = _store.get_manifest(plan_name)
            new_role_config = {
                **(manifest.get("role_config") or {}),
                role: {
                    k: v
                    for k, v in {"provider": provider, "model": model}.items()
                    if v is not None
                },
            }
            # Validate the NEW role_config pre-save: an undeclared model must
            # never reach disk.
            try:
                role_registry.resolve_role(
                    role,
                    plan_role_config=new_role_config,
                    registry=role_registry.load_registry(),
                    model_fallback=lambda: "sonnet",
                )
            except role_registry.RoleRegistryError as exc:
                return {"ok": False, "error": str(exc)}
            manifest["role_config"] = new_role_config
            _store.save_manifest(plan_name, manifest)
            return {
                "ok": True,
                "plan": plan_name,
                "role": role,
                "role_config": new_role_config.get(role),
            }

    def pause_plan(self, plan_name: str) -> dict[str, Any]:
        _validate_key(plan_name)
        return _set_plan_paused(plan_name, True)

    def resume_plan(self, plan_name: str) -> dict[str, Any]:
        _validate_key(plan_name)
        return _set_plan_paused(plan_name, False)


    def list_decisions(self, plan_name: str) -> list[dict]:
        """Return the overlord decision log for a plan (audit trail)."""
        _validate_key(plan_name)
        path = _decisions_path(plan_name)
        return json.loads(path.read_text()) if path.exists() else []

    def get_notifications(self, plan_name: str) -> list[str]:
        _validate_key(plan_name)
        return _store.get_notifications(plan_name)

    def get_notification_records(self, plan_name: str, limit: int = 100) -> list[dict]:
        _validate_key(plan_name)
        return _store.get_notification_records(plan_name, limit)

    def get_decisions(self, plan_name: str) -> list[dict]:
        _validate_key(plan_name)
        return _store.get_decisions(plan_name)

    def get_manifest_or_none(self, plan_name: str) -> dict | None:
        _validate_key(plan_name)
        return _store.get_manifest_or_none(plan_name)

    def list_plans(self) -> list[str]:
        return [p.stem for p in PLAN_DIR.glob("*.json")]

    def resolve_workspace(self, path: str | None, create: bool = False) -> dict:
        """Resolve a workspace path.

        Delegates to :func:`pipeline.workspace.validate_workspace` or
        :func:`pipeline.workspace.create_workspace` based on ``create``.
        If the result has ``ok`` true, records the resolved path via
        ``self._store.add_recent_workspace``.
        Returns the result dict unchanged.
        """
        import pipeline.workspace as _workspace
        if create:
            result = _workspace.create_workspace(path)
        else:
            result = _workspace.validate_workspace(path)
        if result.get("ok"):
            # The result may or may not contain a path; use the one returned
            # or fall back to the input.
            resolved_path = result.get("path") or path
            if resolved_path:
                _store.add_recent_workspace(resolved_path)
        return result

    def list_workspaces(self) -> list[dict]:
        """Return a combined list of recent and manifest workspaces.

        Each entry is ``{"path": str, "exists": bool, "valid": bool}``.
        Recents are returned first in the order provided by
        ``self._store.get_recent_workspaces``.  Manifest-only entries are
        appended after, preserving the order returned by the manifest
        discovery helper.  Paths are de‑duplicated by resolved path.
        """
        import pipeline.workspace as _workspace
        seen = set()
        workspaces: list[dict] = []
        # Recents first
        for p in _store.get_recent_workspaces():
            if p in seen:
                continue
            seen.add(p)
            exists = os.path.exists(p)
            valid = False
            if exists:
                valid = _workspace.validate_workspace(p).get("ok")
            workspaces.append({"path": p, "exists": exists, "valid": valid})
        # Manifest-only
        for plan_name in self.list_plans():
            # strip any .manifest suffix to match manifest filenames
            plan_name = plan_name.removesuffix(".manifest")
            manifest = self.get_manifest_or_none(plan_name)
            if not manifest:
                continue
            repo_root = manifest.get("repo_root")
            if not repo_root:
                continue
            if repo_root in seen:
                continue
            seen.add(repo_root)
            exists = os.path.exists(repo_root)
            valid = False
            if exists:
                valid = _workspace.validate_workspace(repo_root).get("ok")
            workspaces.append({"path": repo_root, "exists": exists, "valid": valid})
        return workspaces

    def get_active_workspace(self) -> str | None:
        """Return the active workspace path, or None if unset.

        The durable record is untrusted input: ``active_workspace.json`` may
        have been hand-edited or corrupted, so a stored spelling that would
        be rejected at the boundary must read as UNSET rather than be handed
        back to callers (and to the operator's UI) as a live selection. The
        stored string is therefore re-checked against the primary control on
        every read.

        Existence is deliberately NOT required here: a workspace whose
        directory has since been deleted is stale, not hostile, and must
        still be reported so the save/decompose fallbacks re-validate and
        fail closed on it at use time rather than silently proceeding as if
        nothing were selected.

        Only a security rejection maps to ``None``; any other error
        propagates, so an unexpected failure surfaces instead of quietly
        degrading into "no workspace selected".
        """
        path = _store.get_active_workspace()
        if path is None:
            return None
        try:
            normalize_workspace_path(path)
        except ValueError:
            return None
        return path

    def read_workspace_file(self, relative_path: str, *, max_bytes: int = 200_000) -> dict:
        """Read a UTF-8 text file from the active workspace.

        Resolves ``relative_path`` inside the active workspace via
        :func:`pipeline.workspace_fs.resolve_within_workspace` and returns
        its contents as text.  Every failure mode returns a dict with
        ``ok`` false and a fixed generic error string; the resolved or
        rejected path is never echoed back to the caller.  The size guard
        runs before the file is opened, so an oversized file is never read
        into memory.
        """
        active = self.get_active_workspace()
        if active is None:
            return {'ok': False, 'error': 'no active workspace'}
        import pipeline.workspace_fs as _workspace_fs
        try:
            resolved = _workspace_fs.resolve_within_workspace(active, relative_path)
        except ValueError:
            return {'ok': False, 'error': 'invalid path'}
        if not os.path.isfile(resolved):
            return {'ok': False, 'error': 'not found'}
        if os.path.getsize(resolved) > max_bytes:
            return {'ok': False, 'error': 'file too large'}
        try:
            with open(resolved, 'r', encoding='utf-8') as fh:
                content = fh.read()
        except UnicodeDecodeError:
            return {'ok': False, 'error': 'not a text file'}
        return {'ok': True, 'path': relative_path, 'content': content}

    def list_workspace_directory(self, relative_path: str = '') -> dict:
        """List the immediate children of a directory in the active workspace.

        Mirrors :meth:`read_workspace_file`'s error-shape conventions: every
        failure mode returns a dict with ``ok`` false and a fixed generic
        error string; the resolved or rejected path is never echoed back to
        the caller.  Only IMMEDIATE children are listed -- the listing never
        recurses into subdirectories (unbounded recursion is a
        resource-exhaustion risk).

        ``relative_path`` of ``''`` (the default) or ``'.'`` addresses the
        workspace root itself; ``resolve_within_workspace`` rejects the empty
        spelling outright, so the empty path is mapped onto ``'.'`` before
        resolution.
        """
        active = self.get_active_workspace()
        if active is None:
            return {'ok': False, 'error': 'no active workspace'}
        import pipeline.workspace_fs as _workspace_fs
        # resolve_within_workspace rejects the empty spelling outright; the
        # brief mandates that '' addresses the workspace ROOT, so map it onto
        # the resolver's canonical root spelling for resolution only -- the
        # echoed 'path' stays the caller's original spelling.  Any other
        # non-str value passes through unchanged and is rejected below.
        resolve_spelling = '.' if relative_path == '' else relative_path
        try:
            resolved = _workspace_fs.resolve_within_workspace(
                active, resolve_spelling
            )
        except ValueError:
            return {'ok': False, 'error': 'invalid path'}
        if not os.path.exists(resolved):
            return {'ok': False, 'error': 'not found'}
        if not os.path.isdir(resolved):
            return {'ok': False, 'error': 'not a directory'}
        entries = []
        with os.scandir(resolved) as scan:
            for entry in scan:
                entries.append(
                    {
                        'name': entry.name,
                        'type': 'dir' if entry.is_dir() else 'file',
                    }
                )
        entries.sort(key=lambda e: e['name'])
        return {'ok': True, 'path': relative_path, 'entries': entries}

    def search_workspace(self, pattern: str, *, max_results: int = 200) -> dict:
        """Search the active workspace with grep and return matching lines.

        Mirrors :meth:`read_workspace_file` / :meth:`list_workspace_directory`'s
        error-shape conventions: every failure mode returns a dict with ``ok``
        false and a fixed generic error string; grep's stderr, the workspace
        path, and the pattern are never echoed back to the caller.

        The grep command is passed to :func:`subprocess.run` as a LIST argv --
        never ``shell=True`` and never string-interpolated -- so *pattern*
        travels as a single argv element and is structurally incapable of shell
        injection regardless of its content.  grep's exit code 1 means "nothing
        matched" and is reported as a successful empty search, not an error.
        """
        active = self.get_active_workspace()
        if active is None:
            return {'ok': False, 'error': 'no active workspace'}
        if not isinstance(pattern, str) or '\x00' in pattern or pattern == '':
            return {'ok': False, 'error': 'invalid pattern'}
        try:
            result = subprocess.run(
                [
                    'grep',
                    '-rn',
                    '-I',
                    '--exclude-dir=.git',
                    '-e',
                    pattern,
                    '--',
                    active,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {'ok': False, 'error': 'search timed out'}
        except FileNotFoundError:
            return {'ok': False, 'error': 'search failed'}
        if result.returncode == 0:
            lines = result.stdout.splitlines()
            truncated = len(lines) > max_results
            return {
                'ok': True,
                'matches': lines[:max_results],
                'truncated': truncated,
            }
        if result.returncode == 1:
            return {'ok': True, 'matches': [], 'truncated': False}
        return {'ok': False, 'error': 'search failed'}

    def set_active_workspace(self, path: str | None) -> None:
        """Set the active workspace; None clears it."""
        _store.set_active_workspace(path)

    def approve_merge(self, plan_name: str, story_key: str) -> dict[str, Any]:
        return _approve_merge_impl(plan_name, story_key)

    def ingest_plan(self, plan_name: str, only_epics: list[str] | None = None, overwrite: bool = False) -> dict[str, Any]:
        return _ingest_plan_impl(plan_name, only_epics=only_epics, overwrite=overwrite)

    def request_decision(self,
        plan_name: str,
        story_key: str,
        question: str,
        options: list[str],
        context: str = "",
    ) -> dict[str, Any]:
        """
        Escalate a blocking decision to the overlord, which rules on the user's
        behalf per the decision policy. The ruling is appended to the plan's
        decisions log (audit trail) and returned. Call this from a story agent
        when you are blocked on a choice the user would normally make.
        """
        _validate_key(plan_name)
        _validate_key(story_key)
        with _scoped_repo_root(plan_name):
            policy = _load_policy()
        opts = "\n".join(f"  - {o}" for o in options)
        prompt = (
            f"A pipeline agent working on story {story_key} is blocked on a decision.\n\n"
            f"QUESTION: {question}\n\n"
            f"OPTIONS:\n{opts}\n\n"
            f"CONTEXT: {context}\n\n"
            f"DECISION POLICY:\n{policy}\n\n"
            f"Rule now, using your output contract exactly."
        )
        ruling = _parse_ruling(
            _invoke_overlord(prompt, plan_role_config=_plan_role_config(plan_name))
        )
        record = {
            "story_key": story_key,
            "question": question,
            "options": list(options),
            **ruling,
            "decided_by": "overlord",
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
        _store.append_decision(plan_name, record)
        return record


    def mark_story_in_progress(self, plan_name: str, story_key: str) -> dict[str, Any]:
        _validate_key(plan_name)
        _validate_key(story_key)
        get_ticket_provider().set_state(story_key, LogicalState.IN_PROGRESS, plan_name)

        manifest = _store.get_manifest(plan_name)
        if story_key not in manifest["stories"]:
            return {"ok": False, "error": f"No such story {story_key}"}
        manifest["stories"][story_key]["status"] = "in_progress"
        _store.save_manifest(plan_name, manifest)
        # manifest_path = PLAN_DIR / f"{plan_name}.manifest.json", _atomic_write_json
        return {"ok": True}
    def checkpoint(self,
                   plan_name: str,
                   story_key: str,
                   step: str,
                   summary: str,
                   next_hint: str = "",
                  ) -> dict[str, Any]:
        return _checkpoint_impl(plan_name, story_key, step, summary, next_hint)

    




    def interrupt_story(self, plan_name: str, story_key: str) -> dict[str, Any]:
        _validate_key(plan_name)
        _validate_key(story_key)
        with _store.transaction(plan_name) as acquired:
            if not acquired:
                return {
                    "ok": True,
                    "skipped": "locked",
                    "reason": "another dispatch/interrupt is in progress for this plan",
                }
            manifest_path = _store.manifest_path(plan_name)
            manifest = _store.get_manifest(plan_name)
            story = manifest["stories"].get(story_key)
            if not story:
                return {"ok": False, "error": f"No such story {story_key}"}
            if "pid" not in story:
                return {"ok": False, "error": "Story not dispatched"}
            sha = _terminate_and_checkpoint(
                manifest,
                manifest_path,
                plan_name,
                story_key,
                story,
                pid=story["pid"],
                step="interrupted",
                summary="Agent process terminated; checkpointed for resume.",
            )
            return {"ok": True, "status": "interrupted", "commit": sha}

    def list_ready_stories(self, plan_name: str) -> list[dict]:
        _validate_key(plan_name)
        if not _store.manifest_path(plan_name).exists():
            return []

        # keep reference to PLAN_DIR for free variable test
        manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"  # noqa: F841

        manifest = _store.get_manifest(plan_name)
        stories = manifest["stories"]
        done = _completed_dep_ids(stories)

        ready = []
        for key, story in stories.items():
            if story["status"] != "todo":
                continue
            deps_met = all(dep in done for dep in story["dependencies"])
            if deps_met:
                ready.append({"key": key, "summary": story["summary"]})
        return ready
    def save_plan(self, plan_name: str, plan_json: str, workspace: str | None = None) -> dict[str, Any]:
        _validate_key(plan_name)
        try:
            plan = json.loads(plan_json)
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"Invalid JSON: {e}"}
        if not isinstance(plan, dict):
            return {"ok": False, "error": "Plan JSON must be a JSON object"}

        if workspace is not None:
            ws_result = validate_workspace(workspace)
            if not ws_result.get("ok"):
                return {
                    "ok": False,
                    "error": ws_result.get("error") or "invalid workspace",
                }
            # WS-11: the model-authored repo_root is untrusted -- overwrite it
            # with the server-validated resolved path.
            plan["repo_root"] = ws_result["path"]

        if "epics" not in plan:
            return {"ok": False, "error": "Plan must contain 'epics' key"}

        path = PLAN_DIR / f"{plan_name}.json"
        _atomic_write_json(path, plan)

        story_count = sum(len(e.get("stories", [])) for e in plan["epics"])
        return {
            "ok": True,
            "path": str(path),
            "epic_count": len(plan["epics"]),
            "story_count": story_count,
        }
    def advance_all_plans(self) -> dict[str, Any]:
        plans = {}
        for plan_name in _store.list_manifests():
            try:
                plans[plan_name] = advance_pipeline(plan_name)
            except Exception as e:  # noqa: BLE001 (one plan's failure must not stop every other plan's tick, per the comment below)
                # One plan's failure (bad repo_root, missing tool, transient git
                # error, ...) must not stop every other plan from getting its tick.
                plans[plan_name] = {"ok": False, "error": str(e)}
        return {"ok": True, "plans": plans}

    def get_role_config(self, plan_name: str | None = None) -> dict[str, Any]:
        plan_role_config = _plan_role_config(plan_name) if plan_name else None
        role_fallbacks = {
            "overlord": lambda: _persona_default_model("overlord") or "opus",
            "planner": lambda: DEFAULT_MODEL,
            "dispatch": lambda: DEFAULT_MODEL,
            "review": lambda: _persona_default_model("code-reviewer") or DEFAULT_MODEL,
            "decompose": lambda: _persona_default_model("product-analyst") or "opus",
        }
        roles = {}
        for role, fallback in role_fallbacks.items():
            resolution = role_registry.resolve_role(
                role,
                plan_role_config=plan_role_config,
                model_fallback=fallback,
            )
            roles[role] = {"provider": resolution.provider, "model": resolution.model}
        return {"ok": True, "roles": roles}

    def get_journal(self, plan_name: str, story_key: str) -> tuple[bool, list[dict]]:
        return _store.get_journal(plan_name, story_key)

    def get_journal_final_ts(self, plan_name: str, story_key: str) -> str | None:
        return _store.get_journal_final_ts(plan_name, story_key)

    def get_story_log(self, plan_name: str, story_key: str, manifest: dict[str, Any], lines: int = 200) -> dict[str, Any]:
        return _store.get_story_log(plan_name, story_key, manifest, lines=lines)

    def get_worktree_file(self, story: dict[str, Any], filename: str) -> dict[str, Any]:
        return _store.get_worktree_file(story, filename)

    def decompose_plan(self, request: str, workspace: str | None = None) -> dict[str, Any]:
        text, backend_error = _run_decompose_detailed(request)
        if not text:
            error = "decompose backend returned no output"
            if backend_error:
                error = f"{error} ({backend_error})"
            return {"ok": False, "error": error}
        candidate = _extract_json_block(text)
        try:
            plan = json.loads(candidate)
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"invalid JSON: {e}", "raw": text}
        if not isinstance(plan, dict) or not isinstance(plan.get("epics"), list):
            return {
                "ok": False,
                "error": "response JSON is missing an 'epics' list",
                "raw": text,
            }
        if workspace is not None:
            validated = validate_workspace(workspace)
            if not validated.get("ok"):
                return {"ok": False, "error": validated.get("error") or "invalid workspace"}
            plan["repo_root"] = validated["path"]
        return {"ok": True, "plan": plan}

    def set_story_status(self, plan_name: str, story_key: str, status: str) -> dict[str, Any]:
        _validate_key(plan_name)
        _validate_key(story_key)
        if status not in _VALID_STORY_STATUSES:
            return {
                "ok": False,
                "error": f"invalid status {status!r}: "
                f"must be one of {sorted(_VALID_STORY_STATUSES)}",
            }

        with _store.transaction(plan_name) as acquired:
            if not acquired:
                return {
                    "ok": True,
                    "skipped": "locked",
                    "reason": "another dispatch/ingest/interrupt is in progress for this plan",
                }
            manifest = _store.get_manifest(plan_name)
            story = manifest["stories"].get(story_key)
            if story is None:
                return {"ok": False, "error": f"No such story {story_key!r}"}
            story["status"] = status
            if status != "parked":
                story.pop("parked_reason", None)
            _store.save_manifest(plan_name, manifest)
            return {"ok": True, "story_key": story_key, "status": status}
# manifest_path = PLAN_DIR / f"{plan_name}.manifest.json"
    def patch_story(
        self, plan_name: str, story_key: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Edit a story's plan-authored fields (agent_instructions, model, persona,
        risk, dependencies, acceptance, pr_url, summary) without hand-editing the
        manifest JSON.

        Hand-editing the manifest directly races the scheduler's 60s
        advance_all_plans tick - a read-modify-write on either side can silently
        clobber the other's write. This tool acquires the same _plan_lock the
        scheduler and dispatch_story use, so the edit is atomic with respect to
        it. Only the fields above may be set; status transitions go through
        set_story_status, not this tool.
        """
        _validate_key(plan_name)
        _validate_key(story_key)
        unknown = set(fields) - _PATCHABLE_STORY_FIELDS
        if unknown:
            return {
                "ok": False,
                "error": f"cannot patch field(s) {sorted(unknown)}: "
                f"only {sorted(_PATCHABLE_STORY_FIELDS)} are editable",
            }

        if "backend" in fields and fields["backend"] not in _VALID_STORY_BACKENDS:
            return {
                "ok": False,
                "error": (
                    f"invalid backend {fields['backend']!r}: must be one of "
                    f"{sorted(_VALID_STORY_BACKENDS)}"
                ),
            }

        with _store.transaction(plan_name) as acquired:
            if not acquired:
                return {
                    "ok": True,
                    "skipped": "locked",
                    "reason": "another dispatch/ingest/interrupt is in progress for this plan",
                }
            story = _store.update_story(plan_name, story_key, fields)
            if story is None:
                return {"ok": False, "error": f"No such story {story_key!r}"}
            return {"ok": True, "story_key": story_key, "story": story}

    def dispatch_story(self, plan_name: str, story_key: str) -> dict[str, Any]:
        return _dispatch_story_impl(plan_name, story_key)

    def review_story(self, plan_name: str, story_key: str) -> dict[str, Any]:
        _validate_key(plan_name)
        _validate_key(story_key)
        with _store.transaction(plan_name) as acquired:
            if not acquired:
                return {
                    "ok": True,
                    "skipped": "locked",
                    "reason": "another dispatch/ingest/interrupt/review is in progress for this plan",
                }
            return _original_review_story(plan_name, story_key)
    def advance_pipeline(self, plan_name: str) -> dict[str, Any]:
        _validate_key(plan_name)
        with _store.transaction(plan_name) as acquired:
            if not acquired:
                return {
                    "ok": True,
                    "skipped": "locked",
                    "reason": "another advance_pipeline tick is already running for this plan",
                }
            return _advance_pipeline_locked(plan_name)
    def mark_story_done(self, plan_name: str, story_key: str) -> dict[str, Any]:
        return _mark_story_done_impl(plan_name, story_key)

    def check_usage(self) -> dict[str, Any]:
        return _check_usage_impl()
