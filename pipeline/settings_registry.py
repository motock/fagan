"""Registry of the tenant settings keys and how each one combines across layers.

Declarative only: ``pipeline.settings_resolve`` applies the layer rules.
``kind`` is one of ``lock`` (read only from the deployment layer),
``ceiling`` (a layer may only tighten, unless the layer above opts in with
``allow_loosen``) or ``pref`` (the most specific valid layer wins).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pipeline.test_scope import TEST_SCOPE_VALUES

LAYERS = ("deployment", "org", "team", "team_repo")
REQUEST_LAYER = "request"

_ALL_LAYERS = LAYERS
_TEST_SCOPE_LAYERS = LAYERS + (REQUEST_LAYER,)


def _on_is_tighter(a: bool, b: bool) -> bool:
    return a or b


def _lower_is_tighter(a: int, b: int) -> int:
    return min(a, b)


def _test_scope_tighter(a: str, b: str) -> str:
    return min(a, b, key=TEST_SCOPE_VALUES.index)


@dataclass(frozen=True)
class SettingKey:
    name: str
    type: str
    kind: str
    safe_value: Any
    allowed_layers: tuple[str, ...]
    legacy_env: str
    tighter: Callable[[Any, Any], Any] | None = None
    choices: tuple[str, ...] = ()

    def accepts(self, value: Any) -> bool:
        if self.type == "bool":
            return isinstance(value, bool)
        if self.type == "int":
            # Timeouts and worker counts are positive; 0 and negatives are invalid.
            return isinstance(value, int) and not isinstance(value, bool) and value >= 1
        return isinstance(value, str) and value in self.choices


REGISTRY: dict[str, SettingKey] = {
    key.name: key
    for key in (
        SettingKey(
            name="merge_ci_gate",
            type="bool",
            kind="ceiling",
            safe_value=True,
            allowed_layers=_ALL_LAYERS,
            legacy_env="PIPELINE_MERGE_CI_GATE",
            tighter=_on_is_tighter,
        ),
        SettingKey(
            name="merge_build_gate",
            type="bool",
            kind="ceiling",
            safe_value=True,
            allowed_layers=_ALL_LAYERS,
            legacy_env="PIPELINE_MERGE_BUILD_GATE",
            tighter=_on_is_tighter,
        ),
        SettingKey(
            name="test_scope",
            type="enum",
            kind="ceiling",
            safe_value=TEST_SCOPE_VALUES[0],
            allowed_layers=_TEST_SCOPE_LAYERS,
            legacy_env="PIPELINE_TEST_SCOPE",
            tighter=_test_scope_tighter,
            choices=TEST_SCOPE_VALUES,
        ),
        SettingKey(
            name="merge_ci_timeout_s",
            type="int",
            kind="pref",
            safe_value=900,
            allowed_layers=_ALL_LAYERS,
            legacy_env="PIPELINE_MERGE_CI_TIMEOUT",
        ),
        SettingKey(
            name="merge_readiness_timeout_s",
            type="int",
            kind="pref",
            safe_value=180,
            allowed_layers=_ALL_LAYERS,
            legacy_env="PIPELINE_MERGE_READINESS_TIMEOUT",
        ),
        SettingKey(
            name="rework_on_ci_fail",
            type="bool",
            kind="pref",
            safe_value=False,
            allowed_layers=_ALL_LAYERS,
            legacy_env="PIPELINE_REWORK_ON_CI_FAIL",
        ),
        SettingKey(
            name="reverify_full_suite",
            type="bool",
            kind="pref",
            safe_value=False,
            allowed_layers=_ALL_LAYERS,
            legacy_env="PIPELINE_REVERIFY_FULL_SUITE",
        ),
        SettingKey(
            name="grade_xdist_workers",
            type="int",
            kind="ceiling",
            safe_value=1,
            allowed_layers=_ALL_LAYERS,
            legacy_env="PIPELINE_GRADE_XDIST_WORKERS",
            tighter=_lower_is_tighter,
        ),
    )
}
