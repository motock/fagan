"""Where the dashboard serves its HTTP API contract.

The API-key gate in ``app.dashboard`` is middleware scoped to the ``/api/``
path prefix (plus the Host check that runs ahead of it). FastAPI's own
documentation endpoints — ``/openapi.json``, ``/docs`` and ``/redoc`` — are
installed by the application object itself, so at their default paths they sit
*outside* that gate: an unauthenticated caller can read every route and schema.

The spec therefore lives under ``/api/`` (``SPEC_PATH``), which puts it behind
the same credential as every other ``/api/`` route by construction, and the two
interactive pages are off by default. Public documentation is an explicit
operator opt-in: ``PIPELINE_DASHBOARD_PUBLIC_DOCS`` set to exactly ``1``
restores FastAPI's defaults. Every other value — unset, empty, ``true``,
``yes``, ``0`` — fails closed, because a typo in an environment variable must
not silently publish the contract.

Standard library only: this module is imported by ``app.dashboard`` at import
time and must not drag in the pipeline, FastAPI, or any application module.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

#: The operator opt-in that makes the documentation pages public again.
PUBLIC_DOCS_ENV = "PIPELINE_DASHBOARD_PUBLIC_DOCS"

#: Where the spec is served when it is not public: under the authenticated
#: ``/api/`` prefix, so the existing gate covers it without a second code path.
SPEC_PATH = "/api/openapi.json"

#: The semantic version of the HTTP surface, stamped into ``info.version``.
#: MAJOR changes only for breaking changes: ``docs/api/openapi.json`` is the
#: snapshot of the surface and ``scripts/api_contract.py check`` refuses a
#: breaking difference that a MAJOR bump does not license.
API_VERSION = "1.0.0"

#: The only environment value that means "publish"; compared after stripping
#: surrounding whitespace so ``PIPELINE_DASHBOARD_PUBLIC_DOCS=" 1 "`` (a common
#: shell/Compose artifact) still opts in, while ``true``/``yes``/``0`` do not.
_PUBLIC_VALUE = "1"


def docs_kwargs(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """FastAPI keyword arguments for the documentation endpoints.

    Always carries ``version`` (``API_VERSION``), so the served spec's
    ``info.version`` is the contract version rather than FastAPI's placeholder.

    When the operator opted in with exactly ``1`` the rest is FastAPI's public
    defaults: ``/openapi.json``, ``/docs`` and ``/redoc``. Otherwise it returns
    the closed configuration: the spec at ``SPEC_PATH``, both interactive pages
    disabled.

    ``environ`` exists so callers can grade a hypothetical environment without
    mutating (or racing on) ``os.environ``; it defaults to ``os.environ`` when
    ``None``. An empty mapping is a valid "nothing is configured", so the
    ``environ or os.environ`` idiom would be wrong here. Nothing is cached: the
    answer must follow the environment it is asked about.
    """
    source = os.environ if environ is None else environ
    if source.get(PUBLIC_DOCS_ENV, "").strip() == _PUBLIC_VALUE:
        return {"version": API_VERSION}
    return {
        "openapi_url": SPEC_PATH,
        "docs_url": None,
        "redoc_url": None,
        "version": API_VERSION,
    }
