"""Layered authority matrix: load, fold and resolve the autonomy tier per class.

The matrix is versioned JSON data, not code. An **org** layer defines every
rule in full (type, bounds, delegation, current value) and may declare a
``never`` list of rule ids that no team layer can ever touch. A **team** layer
carries only ``{"id": ..., "value": ...}`` per rule: it can narrow the value the
org layer allows, never widen it, and it cannot change a rule's type, bounds,
delegation or class.

Folding is sequential and fail-closed. Each team rule is compared against the
value accumulated **so far**; a value outside its envelope is ignored (the
stricter value stays), recorded in :attr:`Resolved.violations` and never
raised. A rejected value is never written into the running state, so a second
team layer is compared against what actually survived - not against the
rejected attempt. Nothing is applied partially: one rule is judged at a time.

Every unknown - unknown decision class, missing resolution, unknown tier
string - resolves to ``human`` through :func:`tier_for`. Core knows only the
opaque namespace ``default``: one team layer per org, no tenant resolution.

Violations are **returned**, not sent: the alert to team leaders that would
consume them is wired in a later story. This module ships inert data; nothing
in the pipeline consults it yet.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: File name used when no explicit or configured org layer is named.
DEFAULT_ORG_FILENAME = "overlord-authority.json"

#: Environment variables naming the two layers (read on every ``resolve()``).
ORG_ENV_VAR = "PIPELINE_AUTHORITY_MATRIX"
TEAM_ENV_VAR = "PIPELINE_AUTHORITY_TEAM_LAYER"

LAYER_ORG = "org"
LAYER_TEAM = "team"
LAYER_NAMES = (LAYER_ORG, LAYER_TEAM)
SUPPORTED_VERSION = 1

DELEGATIONS = frozenset({"locked", "tighten_only", "adjustable"})
RULE_TYPES = frozenset({"ordered_enum", "numeric", "set", "flag"})
TIERS = ("human", "auto+notify", "auto")
HUMAN_TIER = "human"

TEAM_RULE_KEYS = frozenset({"id", "value"})
ORG_RULE_REQUIRED_KEYS = ("id", "class", "type", "value", "delegation")

#: Sentinel distinguishing "no team path given" (fall back to the env var) from
#: an explicit ``None`` (no team layer at all).
_UNSET = object()


class MatrixError(ValueError):
    """An authority-matrix layer is missing, unreadable or schema-invalid."""


@dataclass
class Layer:
    """One parsed and validated authority-matrix layer file.

    ``rules`` maps a rule id to its definition. For an org layer that is the
    full definition (type, bounds, delegation, value); for a team layer it is
    the minimal ``{"id": ..., "value": ...}`` pair. ``raw`` is the file's exact
    bytes, which is what layer digests are taken over.
    """

    layer: str
    version: int
    rules: dict[str, dict[str, Any]]
    never: list[str]
    raw: bytes


@dataclass
class Resolved:
    """The folded matrix: effective values, rejected attempts and layer hashes.

    ``values`` maps rule id to the effective value after every layer was
    folded; ``violations`` lists each ignored team attempt as
    ``{"layer", "rule_id", "attempted", "reason"}``; ``layer_shas`` holds the
    sha256 hex digest of each layer file's raw bytes, org layer first.
    """

    values: dict[str, Any]
    violations: list[dict[str, Any]]
    layer_shas: list[str]


def load_layer(path: str | os.PathLike[str]) -> Layer:
    """Read and validate one layer file, returning a :class:`Layer`.

    Raises :class:`MatrixError` for an unreadable/missing file, malformed JSON
    or any schema violation (unknown type or delegation, missing key, duplicate
    ``values``, ``loosest`` outside ``values``, ``value`` outside its bounds,
    ``min`` above ``max``, or a team rule carrying more than ``id``/``value``).
    """
    name = _basename(path)
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise MatrixError(f"{name}: unreadable ({type(exc).__name__})") from exc
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MatrixError(f"{name}: invalid JSON ({type(exc).__name__})") from exc
    if not isinstance(payload, dict):
        raise MatrixError(f"{name}: a layer must be a JSON object")
    return _parse_layer(payload, raw, name)


def fold(layers: list[Layer]) -> Resolved:
    """Fold ``layers`` from the org layer down, tightening only.

    The layer whose ``layer`` is ``org`` seeds the state; every other layer is
    applied in input order. A rule id the org layer does not define, an id on
    the org ``never`` list, a ``locked`` rule and any value outside the
    envelope set by ``tighten_only``/``adjustable`` is recorded as a violation
    and the accumulated value is left untouched.
    """
    if not layers:
        raise MatrixError("at least one layer is required")
    org = next((layer for layer in layers if layer.layer == LAYER_ORG), None)
    if org is None:
        raise MatrixError("the authority matrix needs an org layer")

    values: dict[str, Any] = {
        rule_id: rule["value"] for rule_id, rule in org.rules.items()
    }
    layer_shas = [_sha256(org.raw)]
    violations: list[dict[str, Any]] = []

    for layer in layers:
        if layer is org:
            continue
        layer_shas.append(_sha256(layer.raw))
        for rule_id, rule in layer.rules.items():
            attempted = rule["value"]
            definition = org.rules.get(rule_id)
            reason = _rejection_reason(
                definition, attempted, values.get(rule_id), rule_id in org.never
            )
            if reason is not None:
                violations.append(
                    {
                        "layer": layer.layer,
                        "rule_id": rule_id,
                        "attempted": attempted,
                        "reason": reason,
                    }
                )
                continue
            # Only an accepted value reaches the running state, so a rejected
            # attempt can never be the baseline the next layer is judged on.
            values[rule_id] = attempted

    return Resolved(values=values, violations=violations, layer_shas=layer_shas)


def tier_for(resolved: Resolved | None, decision_class: str) -> str:
    """Return the tier for ``decision_class``, failing closed to ``human``.

    An unknown class, an absent resolution and a value that is not one of
    ``human``/``auto+notify``/``auto`` all resolve to ``human``: shipping a
    matrix without a class cannot grant autonomy by omission.
    """
    if resolved is None or not isinstance(decision_class, str):
        return HUMAN_TIER
    value = resolved.values.get(f"{decision_class}.tier")
    if isinstance(value, str) and value in TIERS:
        return value
    return HUMAN_TIER


def _with_unreadable_team_violation(org: Layer) -> Resolved:
    """Fold the org layer alone and record the team layer as unreadable."""
    resolved = fold([org])
    resolved.violations.append({"layer": LAYER_TEAM, "reason": "unreadable"})
    return resolved


class FileLayerSource:
    """Resolve the org layer (and an optional team layer) from files.

    Paths are read lazily inside :meth:`resolve`, never at import time: an
    explicit argument wins, then ``PIPELINE_AUTHORITY_MATRIX``, then
    ``overlord-authority.json`` beside ``overlord-policy.md`` (``POLICY_PATH``
    read lazily, like :mod:`pipeline.overlord` does). The team layer comes from
    ``PIPELINE_AUTHORITY_TEAM_LAYER`` unless one was passed in; unset means no
    team layer. Core serves only the ``default`` namespace, so there is exactly
    one team layer and no tenant resolution.

    A missing or invalid org layer disables the matrix: :meth:`resolve`
    returns ``None`` and logs one ERROR record naming the file's basename and
    the error type - never the file contents. A missing or invalid team layer
    is a single ``{"layer": "team", "reason": "unreadable"}`` violation and the
    org layer alone applies, so a broken team file can only lose authority.
    """

    def __init__(
        self,
        org_path: str | os.PathLike[str] | None = None,
        team_path: str | os.PathLike[str] | None | object = _UNSET,
    ) -> None:
        self._org_path = org_path
        self._team_path = team_path

    def resolve(self) -> Resolved | None:
        """Load the layers and fold them; ``None`` when the org layer is unusable."""
        try:
            org_path = self._org_path_for()
        except (ImportError, AttributeError, OSError, TypeError, ValueError) as exc:
            # Fail closed when even the default path cannot be resolved.
            logger.error(
                "authority matrix unusable (%s): %s",
                DEFAULT_ORG_FILENAME,
                type(exc).__name__,
            )
            return None

        try:
            org = load_layer(org_path)
            if org.layer != LAYER_ORG:
                raise MatrixError(f"{_basename(org_path)}: not an org layer")
        except (MatrixError, OSError) as exc:
            logger.error(
                "authority matrix unusable (%s): %s",
                _basename(org_path),
                type(exc).__name__,
            )
            return None

        try:
            team_path = self._team_path_for()
        except (OSError, TypeError, ValueError):
            # A team path that cannot even be resolved is unreadable data too.
            return _with_unreadable_team_violation(org)
        if team_path is not None:
            try:
                team = load_layer(team_path)
                if team.layer != LAYER_TEAM:
                    raise MatrixError(f"{_basename(team_path)}: not a team layer")
            except (MatrixError, OSError):
                return _with_unreadable_team_violation(org)
            return fold([org, team])

        return fold([org])

    def _org_path_for(self) -> Path:
        if self._org_path is not None:
            return Path(self._org_path)
        configured = os.environ.get(ORG_ENV_VAR)
        if configured:
            return Path(configured)
        return _default_org_path()

    def _team_path_for(self) -> Path | None:
        if self._team_path is not _UNSET:
            if self._team_path is None:
                return None
            return Path(self._team_path)
        configured = os.environ.get(TEAM_ENV_VAR)
        return Path(configured) if configured else None


def _default_org_path() -> Path:
    """``overlord-authority.json`` beside the policy file, resolved lazily."""
    # Lazy import, mirroring pipeline.overlord._load_policy: POLICY_PATH is a
    # server module-level global that tests patch, so it must be read at call
    # time rather than copied at import time.
    from .server import POLICY_PATH

    return Path(POLICY_PATH).parent / DEFAULT_ORG_FILENAME


def _parse_layer(payload: dict[str, Any], raw: bytes, source: str) -> Layer:
    version = payload.get("version")
    if version != SUPPORTED_VERSION:
        raise MatrixError(f"{source}: unsupported version {version!r}")
    kind = payload.get("layer")
    if kind not in LAYER_NAMES:
        raise MatrixError(f"{source}: unknown layer {kind!r}")
    rules_payload = payload.get("rules")
    if not isinstance(rules_payload, list):
        raise MatrixError(f"{source}: rules must be a list")

    rules: dict[str, dict[str, Any]] = {}
    for position, entry in enumerate(rules_payload):
        rule = _parse_rule(entry, kind, source, position)
        rule_id = rule["id"]
        if rule_id in rules:
            raise MatrixError(f"{source}: duplicate rule id {rule_id!r}")
        rules[rule_id] = rule

    never: list[str] = []
    if "never" in payload:
        if kind != LAYER_ORG:
            raise MatrixError(f"{source}: only an org layer may declare a never-list")
        never_payload = payload["never"]
        if not isinstance(never_payload, list) or not all(
            isinstance(item, str) for item in never_payload
        ):
            raise MatrixError(f"{source}: never must be a list of rule ids")
        never = list(never_payload)

    return Layer(layer=kind, version=version, rules=rules, never=never, raw=raw)


def _parse_rule(entry: Any, kind: str, source: str, position: int) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise MatrixError(f"{source}: rule #{position} must be an object")
    rule_id = entry.get("id")
    if not isinstance(rule_id, str) or not rule_id:
        raise MatrixError(f"{source}: rule #{position} needs a non-empty string id")

    if kind == LAYER_TEAM:
        keys = set(entry)
        if keys != TEAM_RULE_KEYS:
            extra = sorted(keys - TEAM_RULE_KEYS)
            missing = sorted(TEAM_RULE_KEYS - keys)
            raise MatrixError(
                f"{source}: team rule {rule_id!r} must carry exactly "
                f"'id' and 'value' (extra={extra}, missing={missing})"
            )
        return {"id": rule_id, "value": entry["value"]}

    for key in ORG_RULE_REQUIRED_KEYS:
        if key not in entry:
            raise MatrixError(f"{source}: rule {rule_id!r} is missing {key!r}")
    rule = dict(entry)
    _validate_org_rule(rule, source)
    return rule


def _validate_org_rule(rule: dict[str, Any], source: str) -> None:
    rule_id = rule["id"]
    rule_type = rule["type"]
    if rule_type not in RULE_TYPES:
        raise MatrixError(f"{source}: rule {rule_id!r} has unknown type {rule_type!r}")
    delegation = rule["delegation"]
    if delegation not in DELEGATIONS:
        raise MatrixError(
            f"{source}: rule {rule_id!r} has unknown delegation {delegation!r}"
        )
    rule_class = rule["class"]
    if not isinstance(rule_class, str) or not rule_class:
        raise MatrixError(f"{source}: rule {rule_id!r} needs a non-empty class")
    value = rule["value"]

    if rule_type == "ordered_enum":
        values = rule.get("values")
        if not isinstance(values, list) or not values:
            raise MatrixError(f"{source}: rule {rule_id!r} needs a non-empty values list")
        if not all(isinstance(item, str) for item in values):
            raise MatrixError(f"{source}: rule {rule_id!r} values must be strings")
        if len(set(values)) != len(values):
            raise MatrixError(f"{source}: rule {rule_id!r} values must be unique")
        bounds = rule.get("bounds")
        if not isinstance(bounds, dict) or "loosest" not in bounds:
            raise MatrixError(f"{source}: rule {rule_id!r} needs bounds.loosest")
        loosest = bounds["loosest"]
        if loosest not in values:
            raise MatrixError(
                f"{source}: rule {rule_id!r} loosest {loosest!r} is not in values"
            )
        if value not in values:
            raise MatrixError(
                f"{source}: rule {rule_id!r} value {value!r} is not in values"
            )
        rule["bounds"] = {"loosest": loosest}
        return

    if rule_type == "numeric":
        tighter = rule.get("tighter")
        if tighter not in ("lower", "higher"):
            raise MatrixError(
                f"{source}: rule {rule_id!r} needs tighter 'lower' or 'higher'"
            )
        bounds = rule.get("bounds")
        if not isinstance(bounds, dict) or "min" not in bounds or "max" not in bounds:
            raise MatrixError(f"{source}: rule {rule_id!r} needs bounds.min and bounds.max")
        low, high = bounds["min"], bounds["max"]
        if not _is_number(low) or not _is_number(high):
            raise MatrixError(f"{source}: rule {rule_id!r} bounds must be numbers")
        if low > high:
            raise MatrixError(f"{source}: rule {rule_id!r} has min above max")
        if not _is_number(value) or not low <= value <= high:
            raise MatrixError(
                f"{source}: rule {rule_id!r} value {value!r} is outside bounds"
            )
        rule["bounds"] = {"min": low, "max": high}
        return

    if rule_type == "set":
        bounds = rule.get("bounds")
        if not isinstance(bounds, dict):
            raise MatrixError(f"{source}: rule {rule_id!r} needs bounds")
        flavors = [key for key in ("must_include", "may_choose_from") if key in bounds]
        if len(flavors) != 1 or set(bounds) != {flavors[0]}:
            raise MatrixError(
                f"{source}: rule {rule_id!r} bounds need exactly one of "
                "'must_include' or 'may_choose_from'"
            )
        flavor = flavors[0]
        bound = bounds[flavor]
        if not isinstance(bound, list):
            raise MatrixError(f"{source}: rule {rule_id!r} bounds.{flavor} must be a list")
        bound_set = _as_set(bound, source, rule_id)
        if not isinstance(value, list):
            raise MatrixError(f"{source}: rule {rule_id!r} value must be a list")
        value_set = _as_set(value, source, rule_id)
        if flavor == "must_include" and not value_set >= bound_set:
            raise MatrixError(
                f"{source}: rule {rule_id!r} value is missing a must_include entry"
            )
        if flavor == "may_choose_from" and not value_set <= bound_set:
            raise MatrixError(
                f"{source}: rule {rule_id!r} value is outside may_choose_from"
            )
        rule["bounds"] = {flavor: list(bound)}
        return

    # flag
    tighter = rule.get("tighter")
    if not isinstance(tighter, bool):
        raise MatrixError(f"{source}: rule {rule_id!r} needs a boolean tighter")
    if not isinstance(value, bool):
        raise MatrixError(f"{source}: rule {rule_id!r} value must be a boolean")


def _rejection_reason(
    definition: dict[str, Any] | None,
    attempted: Any,
    current: Any,
    is_never: bool,
) -> str | None:
    """Return why ``attempted`` is refused, or ``None`` when it may be applied."""
    if definition is None:
        return "unknown_rule"
    if is_never:
        return "never_list"
    delegation = definition["delegation"]
    if delegation == "locked":
        return "locked"
    if _within_envelope(definition, attempted, current, delegation):
        return None
    return "outside_envelope"


def _within_envelope(
    definition: dict[str, Any], attempted: Any, current: Any, delegation: str
) -> bool:
    rule_type = definition["type"]
    bounds = definition.get("bounds") or {}

    if rule_type == "ordered_enum":
        values = definition["values"]
        if attempted not in values:
            return False
        index = values.index(attempted)
        if delegation == "tighten_only":
            # values run tightest -> loosest, so a lower index is tighter.
            return current in values and index <= values.index(current)
        return index <= values.index(bounds["loosest"])

    if rule_type == "numeric":
        if not _is_number(attempted):
            return False
        if delegation == "tighten_only":
            if not _is_number(current):
                return False
            if definition["tighter"] == "lower":
                return attempted <= current
            return attempted >= current
        return bounds["min"] <= attempted <= bounds["max"]

    if rule_type == "set":
        if not isinstance(attempted, list):
            return False
        chosen = set(attempted)
        if "must_include" in bounds:
            if delegation == "adjustable":
                return chosen >= set(bounds["must_include"])
            return isinstance(current, list) and chosen >= set(current)
        if delegation == "adjustable":
            return chosen <= set(bounds["may_choose_from"])
        return isinstance(current, list) and chosen <= set(current)

    # flag
    if not isinstance(attempted, bool):
        return False
    if delegation == "tighten_only":
        if not isinstance(current, bool):
            return False
        return attempted is current or attempted is definition["tighter"]
    return True


def _is_number(value: Any) -> bool:
    """True for a real number; booleans are not numbers here."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_set(items: list[Any], source: str, rule_id: str) -> set[Any]:
    try:
        return set(items)
    except TypeError as exc:
        raise MatrixError(
            f"{source}: rule {rule_id!r} set entries must be hashable"
        ) from exc


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _basename(path: str | os.PathLike[str]) -> str:
    return Path(path).name
