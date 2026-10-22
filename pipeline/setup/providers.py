"""Provider/model routing setup for the guided installer (SETUP-05).

Three public functions, all free of import-time side effects:

``ensure_local_registry``
    Bootstrap ``<repo_root>/model_registry.local.json`` (gitignored) from the
    committed ``model_registry.json``, keeping only its ``providers`` catalog.
    The committed file is never written.

``preset_choices``
    Expand a named preset ("claude", "ollama-local", "ollama-cloud",
    "custom") into one ``{role, provider, model}`` choice per pipeline role.

``apply_choices``
    Point ``PIPELINE_MODEL_REGISTRY_PATH`` at the local registry for the
    duration of the call and hand each choice to
    ``PipelineService.set_role_default`` (or an injected setter), recording a
    per-role outcome instead of aborting on the first failure.

``PIPELINE_ROLES`` and ``PipelineService`` are imported lazily inside the
functions: the CLI imports this module only after the venv dependency guard,
and importing ``pipeline.service`` pulls in the whole server surface.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

LOCAL_REGISTRY_NAME = "model_registry.local.json"
COMMITTED_REGISTRY_NAME = "model_registry.json"
REGISTRY_PATH_ENV = "PIPELINE_MODEL_REGISTRY_PATH"

PRESETS: tuple[str, ...] = ("claude", "ollama-local", "ollama-cloud", "custom")

_CLOUD_TAG_SUFFIX = ":cloud"


def ensure_local_registry(repo_root: str | os.PathLike[str]) -> Path:
    """Return the local registry path, creating it from the committed one.

    An existing local registry is parsed (malformed JSON raises ``ValueError``
    naming the path and leaves the file untouched) and returned unchanged, so
    roles the operator already configured survive a re-run. When absent, the
    committed registry's ``providers`` mapping is copied into a new local file
    written atomically (temp file in the same directory + ``os.replace``).
    """
    root = Path(repo_root)
    target = root / LOCAL_REGISTRY_NAME

    if target.exists():
        try:
            json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"malformed local model registry {target}: {exc}"
            ) from exc
        return target

    committed = root / COMMITTED_REGISTRY_NAME
    data = json.loads(committed.read_text(encoding="utf-8"))
    # Only the provider catalog is operator-local; "roles"/"routing" in the
    # committed file describe shipped defaults, not this operator's choices.
    _atomic_write_json(target, {"providers": data.get("providers") or {}})
    return target


def preset_choices(preset: str, registry: dict) -> list[dict]:
    """Expand ``preset`` into one ``{role, provider, model}`` per pipeline role.

    Raises ``ValueError`` naming the preset when the provider or model it
    needs is not declared in ``registry`` — never guesses a substitute.
    """
    from pipeline.config_provenance import PIPELINE_ROLES

    if preset == "custom":
        return []
    if preset == "claude":
        provider, model = "claude", "sonnet"
    elif preset in ("ollama-local", "ollama-cloud"):
        provider = "ollama"
        model = _ollama_model(preset, registry)
    else:
        raise ValueError(
            f"unknown preset {preset!r}: expected one of {', '.join(PRESETS)}"
        )

    _require_declared(preset, registry, provider, model)
    return [
        {"role": role, "provider": provider, "model": model}
        for role in PIPELINE_ROLES
    ]


def apply_choices(
    choices: list[dict],
    *,
    registry_path: str | os.PathLike[str],
    environ: dict | None = None,
    setter=None,
) -> list[dict]:
    """Apply ``choices`` through the setter, one role at a time.

    ``PIPELINE_MODEL_REGISTRY_PATH`` is set in ``environ`` (default
    ``os.environ``) for the duration of the call and restored to its previous
    value — or removed if it was absent — on exit, including on error. Each
    choice yields ``{role, provider, model, ok, error}``; a bad role, an empty
    provider/model, a raising setter, or a setter result with ``ok=False`` is
    recorded and the remaining roles still run.
    """
    from pipeline.config_provenance import PIPELINE_ROLES

    if environ is None:
        environ = os.environ
    scopes = [environ]
    if setter is None:
        from pipeline.service import PipelineService

        setter = PipelineService().set_role_default
        # The real setter resolves its write target from os.environ, so a
        # caller-supplied mapping must not be the only place the path is
        # visible — otherwise the write would land in the committed registry.
        if environ is not os.environ:
            scopes.append(os.environ)

    path = str(Path(registry_path))
    missing = object()
    previous = [(scope, scope.get(REGISTRY_PATH_ENV, missing)) for scope in scopes]
    for scope in scopes:
        scope[REGISTRY_PATH_ENV] = path
    try:
        return [_apply_one(choice, setter, PIPELINE_ROLES) for choice in choices]
    finally:
        for scope, prior in previous:
            if prior is missing:
                scope.pop(REGISTRY_PATH_ENV, None)
            else:
                scope[REGISTRY_PATH_ENV] = prior


def _apply_one(choice: dict, setter, roles: tuple[str, ...]) -> dict:
    """Validate and apply a single choice, returning its outcome record."""
    role = str(choice.get("role") or "")
    provider = str(choice.get("provider") or "")
    model = str(choice.get("model") or "")
    record = {
        "role": role,
        "provider": provider,
        "model": model,
        "ok": False,
        "error": None,
    }

    if role not in roles:
        record["error"] = f"unknown role {role!r}"
        return record
    if not provider:
        record["error"] = "provider must be non-empty"
        return record
    if not model:
        record["error"] = "model must be non-empty"
        return record

    try:
        outcome = setter(role, provider, model)
    except Exception as exc:  # noqa: BLE001 - one bad role must not abort the rest
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record

    if not isinstance(outcome, dict) or not outcome.get("ok"):
        error = outcome.get("error") if isinstance(outcome, dict) else None
        record["error"] = str(error) if error else "setter reported failure"
        return record

    record["ok"] = True
    return record


def _ollama_model(preset: str, registry: dict) -> str:
    """Return the first ollama model whose tag matches the preset's locality."""
    models = _provider_models(registry, "ollama")
    want_cloud = preset == "ollama-cloud"
    for name, spec in models.items():
        tag = str((spec or {}).get("tag") or "")
        if not tag:
            continue
        if tag.endswith(_CLOUD_TAG_SUFFIX) == want_cloud:
            return name
    locality = "cloud" if want_cloud else "local"
    raise ValueError(
        f"preset {preset!r}: no {locality} ollama model declared under "
        f"providers.ollama.models (need a tag "
        f"{'ending with' if want_cloud else 'not ending with'} "
        f"{_CLOUD_TAG_SUFFIX!r})"
    )


def _provider_models(registry: dict, provider: str) -> dict:
    providers = registry.get("providers") or {}
    return (providers.get(provider) or {}).get("models") or {}


def _require_declared(preset: str, registry: dict, provider: str, model: str) -> None:
    """Fail closed when the preset's provider/model is not declared."""
    providers = registry.get("providers") or {}
    if provider not in providers:
        raise ValueError(
            f"preset {preset!r}: provider {provider!r} is not declared under "
            f"providers"
        )
    if model not in _provider_models(registry, provider):
        raise ValueError(
            f"preset {preset!r}: model {model!r} is not declared under "
            f"providers.{provider}.models"
        )


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write ``payload`` as JSON to ``path`` via a same-directory temp file."""
    text = json.dumps(payload, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
