"""model_registry.json ships as a provider-neutral catalog: no baked-in
per-operator role routing.

Commit 4e11de2 committed `roles` entries pinning all nine pipeline roles
(and `routing.dispatch`'s default tier) to provider `ollama` / model `glm`
(tag `glm-5.3-flash:cloud`, a hosted model proxied through
https://ollama.com requiring an ollama.com account). On a clean environment
this made `app.role_registry.resolve_role("dispatch")` return
`("ollama", "glm-5.3-flash:cloud")` on a fresh clone, contradicting the
README's documented claude-backend default.

Role routing is a per-operator choice, not shared reference data. The
`providers` catalog (which models this repo knows how to address) stays;
`roles` and `routing` (which model each role should use) do not ship.
Operators select their own routing via a `model_registry.local.json`
pointed to by the pre-existing PIPELINE_MODEL_REGISTRY_PATH env var (see
app/role_registry.py's `_registry_path`).
"""
import json
from pathlib import Path

from app import role_registry as rr

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_REGISTRY_PATH = _REPO_ROOT / "model_registry.json"
_GITIGNORE_PATH = _REPO_ROOT / ".gitignore"


def _load_committed_registry() -> dict:
    return json.loads(_REGISTRY_PATH.read_text())


def test_committed_registry_parses_and_has_providers():
    data = _load_committed_registry()
    assert isinstance(data, dict)
    assert data.get("providers")


def test_committed_registry_has_no_roles_key():
    data = _load_committed_registry()
    assert "roles" not in data


def test_committed_registry_has_no_routing_key():
    data = _load_committed_registry()
    assert "routing" not in data


def test_gitignore_lists_local_registry_override():
    lines = _GITIGNORE_PATH.read_text().splitlines()
    assert "model_registry.local.json" in lines


def test_load_registry_from_committed_file_has_no_resolvable_roles():
    """Negative/boundary: loading the shipped file directly must yield no
    role entries at all -- no role can resolve a default from the shipped
    file alone."""
    data = rr.load_registry(path=_REGISTRY_PATH)
    assert data.get("roles", {}) == {}


def test_committed_registry_keeps_every_provider():
    """This story must not pass by emptying the file wholesale -- the
    provider/model catalog itself is genuinely shared reference data and
    must survive untouched."""
    data = _load_committed_registry()
    assert set(data["providers"]) == {
        "claude",
        "ollama",
        "mlx",
        "lmstudio",
        "litellm",
        "local",
    }
