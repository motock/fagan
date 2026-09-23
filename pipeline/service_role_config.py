"""PipelineService.set_plan_role_config and set_role_default bodies, moved out
of pipeline/service.py (line-count target). The methods keep their
signatures and docstrings and delegate here. Server-owned names are
_ServerRef bindings, exactly as in pipeline.service, so tests patching
pipeline.server still land.
"""

import json
import os

from .service import _ServerRef

_store = _ServerRef("_store")
_validate_key = _ServerRef("_validate_key")
config_provenance = _ServerRef("config_provenance")
role_registry = _ServerRef("role_registry")


def _set_plan_role_config_impl(self, plan_name: str, role: str, provider: str | None=None, model: str | None=None):
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


def _set_role_default_impl(self, role: str, provider: str | None, model: str | None):
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
