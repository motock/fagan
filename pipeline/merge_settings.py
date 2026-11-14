"""Settings context for the merge gates: the (namespace, team, repo) a merge
resolves its CI, timeout, build and reverify settings for.

The caller builds one :class:`SettingsContext` from the plan manifest and
passes it down. Nothing here reads a process-global value, so two plans in
one process resolve their own gates and cannot see each other's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .authorization import DEFAULT_TEAM
from .repo_clone import parse_repo_url
from .settings_store import effective_setting

DEFAULT_NAMESPACE = "default"

# The settings a merge reads. Their values and sources go on the merge record.
MERGE_GATE_KEYS = (
    "merge_ci_gate",
    "merge_ci_timeout_s",
    "merge_build_gate",
    "reverify_full_suite",
)


@dataclass(frozen=True)
class SettingsContext:
    """One merge's resolution context. Every field is required."""

    namespace: str
    team: str
    repo: str
    # Each key is read from the Store once per context, so one merge attempt
    # sees one consistent value for each gate.
    _resolved: dict[str, dict[str, Any]] = field(
        default_factory=dict, init=False, compare=False, repr=False
    )

    def resolve(self, key: str) -> dict[str, Any]:
        """Return the resolved value, source, team and repo for ``key``."""
        if key not in self._resolved:
            self._resolved[key] = effective_setting(
                key, namespace=self.namespace, team=self.team, repo=self.repo
            )
        return self._resolved[key]

    def value(self, key: str) -> Any:
        return self.resolve(key)["value"]


def _repo_identity(manifest: dict[str, Any]) -> str:
    """``host/org/name`` for the plan's ``repo_url``, else its ``repo_root``.

    A malformed ``repo_url`` raises :class:`~pipeline.repo_clone.RepoUrlError`
    so the merge fails closed rather than resolving against the wrong repo.
    """
    url = manifest.get("repo_url")
    if url:
        host, owner, name = parse_repo_url(url)
        return f"{host}/{owner}/{name}"
    return manifest.get("repo_root") or ""


def context_from_manifest(manifest: dict[str, Any]) -> SettingsContext:
    """Build the merge context from a plan manifest."""
    return SettingsContext(
        namespace=manifest.get("namespace") or DEFAULT_NAMESPACE,
        team=manifest.get("team") or DEFAULT_TEAM,
        repo=_repo_identity(manifest),
    )


def gate_record(ctx: SettingsContext) -> dict[str, dict[str, Any]]:
    """Per merge gate: the value used and the source, team and repo it came from."""
    record = {}
    for key in MERGE_GATE_KEYS:
        resolved = ctx.resolve(key)
        record[key] = {
            "value": resolved["value"],
            "source": resolved["source"],
            "team": resolved["team"],
            "repo": resolved["repo"],
        }
    return record
