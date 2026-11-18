"""A role's provenance with the plan's ``role_routing`` setting applied.

``config_provenance.resolve_role_provenance`` labels which layer supplied a
role's winning provider/model. The settings layer is one of those sources:
this module reads the plan's ``role_routing`` setting through
:mod:`pipeline.role_routing_scope` (the same read the execution path uses) and
hands the resolved ``(value, source)`` pair to
``config_provenance.effective_role_config_with_routing``, so a role the
settings layer routed is reported as ``settings:<layer>`` instead of stopping
at the registry.

It lives beside - not inside - ``pipeline.config_provenance`` because that
module's import graph is pinned to the standard library plus
``app.role_registry`` (a settings read would pull the Store in).

Reporting only: nothing here changes what provider or model a role actually
runs on. A settings read that fails degrades to no routing rather than raising,
so a diagnostic view never breaks a tick.
"""

from __future__ import annotations

from typing import Any

from . import config_provenance
from .role_routing_scope import layer_routing_setting_for_plan


def effective_plan_role_config(
    plan_name: str,
    *,
    plan_role_config: Any = None,
    registry: Any = None,
    model_fallbacks: Any = None,
    environ: Any = None,
) -> list[dict]:
    """``effective_role_config`` for ``plan_name``, with its layer routing.

    Each entry additionally reports ``settings:<layer>`` as
    ``provider_source``/``model_source`` when the plan's ``role_routing``
    setting supplied the value, naming the settings layer it came from.
    """
    return config_provenance.effective_role_config_with_routing(
        layer_routing_setting_for_plan(plan_name),
        plan_role_config=plan_role_config,
        registry=registry,
        model_fallbacks=model_fallbacks,
        environ=environ,
    )
