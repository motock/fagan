"""The runtime ``Store`` adapter for a plugin record store (ADR 0012 decision 7).

A ``PIPELINE_STORE=plugin`` factory returns a namespaced record store (the
``RecordStore`` surface in :mod:`pipeline.store_records`). The server still
calls the legacy ``Store`` names -- one-argument ``get_manifest``,
``list_plans()``, ``update_story`` and the rest -- so :class:`PluginStore`
expresses each of them over the plugin's namespaced methods with the default
namespace, keeping the semantics of :class:`pipeline.store_sqlite.SqliteStore`.
Any other name is delegated to the plugin, so both surfaces are served.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.plugin_loader import require_methods
from pipeline.store_records import DEFAULT_NAMESPACE, RECORD_STORE_METHODS

#: Every name a plugin record store must provide: the record surface plus the
#: outbox and audit methods the server calls on the runtime store.
PLUGIN_STORE_METHODS = RECORD_STORE_METHODS + (
    "append_outbox",
    "read_outbox",
    "ack_outbox",
    "list_outboxes",
    "outbox_summary",
    "audit_log",
)

_NAMESPACE = DEFAULT_NAMESPACE
# The legacy readers' own window, as SqliteStore applies it.
_LEGACY_LINE_LIMIT = 100
_JOURNAL_DISPLAY_FIELDS = ("step", "summary", "next_hint")
_SEVERITIES = ("info", "warning", "error")


def _legacy_notification(record: dict[str, Any]) -> dict[str, Any]:
    """Return ``record`` in the six-key shape the legacy reader has always produced."""
    severity = record.get("severity")
    severity = str(severity) if severity in _SEVERITIES else "info"
    return {
        "ts": str(record.get("ts", "")),
        "message": str(record.get("message", "")),
        "severity": severity,
        "story_key": record.get("story_key"),
        "event": record.get("event"),
        "dedup_key": record.get("dedup_key"),
    }


class PluginStore:
    """A ``Store`` served by a plugin record store.

    The plugin is held, never copied: ``records()`` returns it, so a namespace
    the plugin refuses is refused through that view as well.
    """

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    def __getattr__(self, name: str) -> Any:
        # Reached only for names this class does not define itself.
        plugin = self.__dict__.get("_plugin")
        if plugin is None:
            raise AttributeError(name)
        return getattr(plugin, name)

    def records(self) -> Any:
        """The namespaced record API: the plugin itself."""
        return self._plugin

    def audit_log(self) -> Any:
        return self._plugin.audit_log()

    # -- manifests -----------------------------------------------------
    def get_manifest(self, plan_name: str) -> dict[str, Any]:
        """Return the plan's manifest, failing when the plugin has none."""
        manifest = self._plugin.get_manifest(_NAMESPACE, plan_name)
        if manifest is None:
            raise FileNotFoundError(f"no manifest for plan {plan_name!r}")
        return manifest

    def get_manifest_or_none(self, plan_name: str) -> dict | None:
        return self._plugin.get_manifest(_NAMESPACE, plan_name)

    def save_manifest(self, plan_name: str, manifest: dict[str, Any]) -> None:
        self._plugin.put_manifest(_NAMESPACE, plan_name, manifest)

    def list_manifests(self) -> list[str]:
        return sorted(self._plugin.list_plans(_NAMESPACE))

    def list_plans(self) -> list[str]:
        """Plans with a manifest or a staged draft, sorted."""
        drafts = self._plugin.list_plan_drafts(_NAMESPACE)
        return sorted(set(self.list_manifests()) | set(drafts))

    def update_story(
        self, plan_name: str, story_key: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Apply ``fields`` to one story and persist; ``None`` for an unknown key."""
        manifest = self.get_manifest(plan_name)
        story = manifest["stories"].get(story_key)
        if story is None:
            return None
        story.update(fields)
        self.save_manifest(plan_name, manifest)
        return story

    # -- transactions --------------------------------------------------
    def transaction(self, plan_name: str, wait_seconds: float = 0.0):
        return self._plugin.transaction(_NAMESPACE, plan_name, wait_seconds)

    # -- decisions and journals ----------------------------------------
    def append_decision(self, plan_name: str, record: dict[str, Any]) -> None:
        self._plugin.append_decision(_NAMESPACE, plan_name, record)

    def get_decisions(self, plan_name: str) -> list[dict]:
        return self._plugin.get_decisions(_NAMESPACE, plan_name)

    def append_journal(
        self, plan_name: str, story_key: str, record: dict[str, Any]
    ) -> None:
        self._plugin.append_journal(_NAMESPACE, plan_name, story_key, record)

    def get_journal(self, plan_name: str, story_key: str) -> tuple[bool, list[dict]]:
        """Return ``(available, entries)`` with the display defaults filled in."""
        entries = self._plugin.get_journal(_NAMESPACE, plan_name, story_key)
        if not entries:
            return False, []
        return True, [
            {
                **entry,
                **{
                    name: entry.get(name)
                    for name in _JOURNAL_DISPLAY_FIELDS
                    if name not in entry
                },
            }
            for entry in entries
        ]

    def get_journal_final_ts(self, plan_name: str, story_key: str) -> str | None:
        entries = self._plugin.get_journal(_NAMESPACE, plan_name, story_key)
        return entries[-1].get("ts") if entries else None

    # -- notifications -------------------------------------------------
    def get_notifications(self, plan_name: str) -> list[str]:
        return self._plugin.get_notification_lines(
            _NAMESPACE, plan_name, _LEGACY_LINE_LIMIT
        )

    def get_notification_records(
        self, plan_name: str, limit: int = _LEGACY_LINE_LIMIT
    ) -> list[dict]:
        records, _malformed = self._plugin.get_notification_history(
            _NAMESPACE, plan_name
        )
        normalized = [_legacy_notification(record) for record in records]
        if limit <= 0:
            limit = _LEGACY_LINE_LIMIT
        return normalized[-limit:]


def build_plugin_store(factory: Callable[[], Any]) -> PluginStore:
    """Call ``factory`` and wrap the record store it returns.

    Refuses a store missing any name in :data:`PLUGIN_STORE_METHODS`. The
    refusal names the missing methods only, never the factory's spec.
    """
    built = factory()
    require_methods(built, PLUGIN_STORE_METHODS, "store")
    return PluginStore(built)
