"""Pure projections for the guided setup's Plane ticketing opt-in.

Two public functions (see ``__all__``):

``build_plane_settings``
    Projects the guided setup's answers onto the five environment keys the
    ticketing layer reads: ``PLANE_BASE``, ``PLANE_API_KEY``,
    ``PLANE_WORKSPACE``, ``PLANE_PROJECT`` and ``PIPELINE_TICKET_PROVIDER``.
    Plane is off unless the caller passes ``enabled=True``; an explicit
    disable returns every ``PLANE_*`` value empty and the provider ``"none"``
    regardless of the credentials supplied, so a previously stored secret is
    cleared rather than carried forward (data minimization).

``describe_plane_status``
    One human-readable line for the setup summary: either ``"Plane disabled"``
    or ``"Plane configured for <workspace>/<project> (API key hidden)"``.

The module is pure: no network, no filesystem access, no ``os.environ``
reads, no logging.  Validation collects every missing or invalid field into a
single ValueError that names each offending key and never its value, so the
API key cannot leak through an exception; the key is returned verbatim in the
dict because the env writer owns secret permissions.
"""

from __future__ import annotations

from collections.abc import Mapping

__all__ = ["build_plane_settings", "describe_plane_status"]

_TICKET_PROVIDERS = frozenset({"auto", "none", "plane"})
_URL_PREFIXES = ("http://", "https://")
_DISABLED_PROVIDER = "none"

# (keyword argument, environment key) in the order the message lists them.
_REQUIRED_FIELDS = (
    ("base", "PLANE_BASE"),
    ("api_key", "PLANE_API_KEY"),
    ("workspace", "PLANE_WORKSPACE"),
    ("project", "PLANE_PROJECT"),
)


def _normalized_provider(ticket_provider: str, *, enabled: bool) -> str:
    """Lowercase and validate ``ticket_provider``; never echoes a secret."""
    provider = (ticket_provider or "").strip().lower()
    if provider == "jira":
        raise ValueError(
            "PIPELINE_TICKET_PROVIDER=jira is not supported by guided setup: "
            "Jira must be configured manually (guided setup collects only "
            "Plane credentials)."
        )
    if provider not in _TICKET_PROVIDERS:
        raise ValueError(
            f"PIPELINE_TICKET_PROVIDER={provider or '<empty>'} is not supported; "
            "choose one of auto, none, plane."
        )
    if provider == "plane" and not enabled:
        raise ValueError(
            "PIPELINE_TICKET_PROVIDER=plane requires Plane to be enabled."
        )
    return provider


def _invalid_fields(
    *, base: str, api_key: str, workspace: str, project: str
) -> list[str]:
    """Every required environment key whose supplied value is unusable."""
    values = {
        "base": base,
        "api_key": api_key,
        "workspace": workspace,
        "project": project,
    }
    invalid: list[str] = []
    for argument, key in _REQUIRED_FIELDS:
        value = (values[argument] or "").strip()
        if not value or (argument == "base" and not value.startswith(_URL_PREFIXES)):
            invalid.append(key)
    return invalid


def build_plane_settings(
    *,
    enabled: bool,
    base: str = "",
    api_key: str = "",
    workspace: str = "",
    project: str = "",
    ticket_provider: str = "none",
) -> dict:
    """Project the guided setup's Plane answers onto the env keys.

    Returns exactly ``PLANE_BASE``, ``PLANE_API_KEY``, ``PLANE_WORKSPACE``,
    ``PLANE_PROJECT`` and ``PIPELINE_TICKET_PROVIDER``.

    With ``enabled=False`` every ``PLANE_*`` value is ``""`` and the provider
    is ``"none"``, whatever credentials were supplied; partial input is not an
    error.  With ``enabled=True`` all four credentials are required and
    ``base`` must be an ``http://`` or ``https://`` URL (trailing ``/``
    characters are stripped).  Every missing or invalid field is collected
    into one ValueError naming each key and never its value.

    ``ticket_provider`` must be ``auto``, ``none`` or ``plane`` after
    lowercasing; ``plane`` requires ``enabled=True`` and ``jira`` is rejected
    with a message pointing at manual configuration.
    """
    provider = _normalized_provider(ticket_provider, enabled=enabled)

    if not enabled:
        return {
            "PLANE_BASE": "",
            "PLANE_API_KEY": "",
            "PLANE_WORKSPACE": "",
            "PLANE_PROJECT": "",
            "PIPELINE_TICKET_PROVIDER": _DISABLED_PROVIDER,
        }

    invalid = _invalid_fields(
        base=base, api_key=api_key, workspace=workspace, project=project
    )
    if invalid:
        raise ValueError(
            "Plane is enabled but these settings are missing or invalid: "
            f"{', '.join(invalid)}. Set them, or disable Plane."
        )

    return {
        "PLANE_BASE": base.strip().rstrip("/"),
        "PLANE_API_KEY": api_key,
        "PLANE_WORKSPACE": workspace,
        "PLANE_PROJECT": project,
        "PIPELINE_TICKET_PROVIDER": provider,
    }


def describe_plane_status(settings: Mapping[str, str]) -> str:
    """One summary line for ``settings``; never includes the API key value.

    Reports ``"Plane disabled"`` when the provider is ``"none"`` (or absent)
    or the API key is empty, and
    ``"Plane configured for <workspace>/<project> (API key hidden)"``
    otherwise.  The key is only ever checked for emptiness.
    """
    provider = (settings.get("PIPELINE_TICKET_PROVIDER") or "").strip().lower()
    api_key = settings.get("PLANE_API_KEY") or ""
    if provider == _DISABLED_PROVIDER or not api_key.strip():
        return "Plane disabled"
    workspace = settings.get("PLANE_WORKSPACE") or ""
    project = settings.get("PLANE_PROJECT") or ""
    return f"Plane configured for {workspace}/{project} (API key hidden)"
