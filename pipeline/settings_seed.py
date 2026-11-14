"""Seed the settings Store from the legacy ``PIPELINE_*`` environment variables.

``python -m pipeline.settings_seed [--dry-run] [--overwrite] [--team T --repo R]``

Reads the eight legacy variables named by the settings registry from the
environment of this process and writes one settings key per variable that is
set. Without ``--team``/``--repo`` the values go to the deployment layer. With
both, they go to a ``team_repo`` record, because a legacy value is container
wide and the operator decides which repository it was meant for.

Each legacy variable is parsed with the rule its old reader used, so seeding
does not change what the running process already does. Writes go through
``settings_tools.set_setting``, so authorization and the host audit chain
apply. The CLI runs as the local operator. Output is one JSON object.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from typing import Any

from pipeline import settings_tools
from pipeline.settings_registry import REGISTRY, SettingKey

#: Bool readers, keyed by settings key, copied from the code that reads each
#: legacy variable. The merge gates (``ci.py``) and the local suite opt-in
#: switch on for any value but "0"; the rework switch (``advance_merge.py``)
#: switches on only for "1".
_BOOL_RULES = {
    "merge_ci_gate": lambda text: text != "0",
    "merge_build_gate": lambda text: text != "0",
    "reverify_full_suite": lambda text: text != "0",
    "rework_on_ci_fail": lambda text: text == "1",
}


def _parse(setting: SettingKey, raw: str) -> Any:
    """Return the typed value for one legacy variable. Raises ``ValueError``."""
    text = raw.strip()
    if setting.type == "bool":
        return _BOOL_RULES[setting.name](text)
    if setting.type == "int":
        return int(text)
    return text


def _target(team: str, repo: str) -> tuple[str, str, str]:
    if team:
        return "team_repo", team, repo
    return "deployment", "", ""


def _entry(setting: SettingKey, **extra: Any) -> dict:
    return {"key": setting.name, "legacy_env": setting.legacy_env, **extra}


def seed_from_env(
    environ: Mapping[str, str],
    *,
    team: str = "",
    repo: str = "",
    dry_run: bool = False,
    overwrite: bool = False,
) -> dict:
    """Write the legacy variables set in *environ* and report every key.

    Returns ``{"ok": True, ...}`` with ``written``, ``planned`` (dry run only),
    ``unchanged`` and ``skipped`` lists, or ``{"ok": False, "reason", "error"}``
    when the Store cannot be read or the layer key is malformed.
    """
    scope, scope_id, repo_name = _target(team, repo)
    current = settings_tools.get_settings(scope, scope_id, repo_name)
    if not current["ok"]:
        return {"ok": False, "reason": current["reason"], "error": current["error"]}

    record = current["record"]
    stored = dict(record["values"]) if record else {}
    version = record["version"] if record else None
    report = {
        "ok": True,
        "scope": scope,
        "scope_id": scope_id,
        "repo": repo_name,
        "dry_run": dry_run,
        "written": [],
        "planned": [],
        "unchanged": [],
        "skipped": [],
    }

    for setting in REGISTRY.values():
        raw = environ.get(setting.legacy_env)
        if raw is None or raw.strip() == "":
            continue
        try:
            value = _parse(setting, raw)
        except ValueError:
            report["skipped"].append(
                _entry(setting, reason="invalid_value", error=f"not a valid {setting.type}")
            )
            continue
        problem = settings_tools.value_problem(setting, value)
        if problem is not None:
            report["skipped"].append(_entry(setting, reason=problem[0], error=problem[1]))
            continue
        if setting.name in stored:
            if stored[setting.name] == value:
                report["unchanged"].append(_entry(setting, value=value))
                continue
            if not overwrite:
                report["skipped"].append(
                    _entry(
                        setting,
                        reason="already_set",
                        error="a different value is stored; pass --overwrite to replace it",
                    )
                )
                continue
        if dry_run:
            report["planned"].append(_entry(setting, value=value))
            continue
        result = settings_tools.set_setting(
            scope, scope_id, repo_name, setting.name, value, version
        )
        if not result["ok"]:
            # An authorization refusal has ``forbidden`` and no ``reason``.
            reason = result.get("reason", "forbidden")
            report["skipped"].append(_entry(setting, reason=reason, error=result["error"]))
            continue
        version = result["new_version"]
        stored[setting.name] = value
        report["written"].append(_entry(setting, value=value, version=version))
    return report


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.settings_seed",
        description="Write the legacy PIPELINE_* settings variables into the settings Store.",
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would be written; write nothing")
    parser.add_argument("--overwrite", action="store_true", help="replace stored values that differ")
    parser.add_argument("--team", default="", help="write a team_repo record for this team (needs --repo)")
    parser.add_argument("--repo", default="", help="repository for the team_repo record (needs --team)")
    args = parser.parse_args(argv)
    if bool(args.team) != bool(args.repo):
        parser.error("--team and --repo must be given together")

    report = seed_from_env(
        os.environ if environ is None else environ,
        team=args.team,
        repo=args.repo,
        dry_run=args.dry_run,
        overwrite=args.overwrite,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
