"""The gateway credentials are tenant-scoped: no deployment-wide env fallback.

``app/credentials.py`` keeps two allowlists: ``_LEGACY_ENV`` (the
``(purpose, name) -> environment variable`` map read by the default
provider) and ``_ENV_CREDENTIAL_NAMES`` (the names a ``purpose="env"``
reference may read). The gateway credentials must appear in NEITHER: a
tenant gateway is configured per namespace, never through a process-wide
environment variable.

The value behind such a credential comes from the file secret provider
(``secret://file/...``), which resolves a ref for any name under the secret
root and does not care which purpose the credential was reached through -
that is what makes a per-tenant key possible without a core change per
backend.
"""

from __future__ import annotations

import os
import textwrap

import pytest

from app.credentials import (
    CredentialError,
    CredentialMissing,
    CredentialRef,
    EnvFileProvider,
    resolve,
    resolve_for_injection,
)

FILE_ROOT_ENV = "PIPELINE_SECRET_FILE_ROOT"


@pytest.fixture(autouse=True)
def _clean_credentials_env(monkeypatch):
    for name in ("PIPELINE_CREDENTIALS_PLUGIN", "PIPELINE_CREDENTIALS_PLUGINS"):
        monkeypatch.delenv(name, raising=False)


def _secret_file(root, relative, value, mode=0o600):
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_gateway_api_key_is_not_a_deployment_wide_env_credential() -> None:
    """``("model", "gateway_api_key")`` is not in the legacy env map."""
    provider = EnvFileProvider({"GATEWAY_API_KEY": "from-the-environment"})

    resolved = provider.get(CredentialRef("model", "gateway_api_key"))

    assert resolved is None, (
        "the gateway key must not be readable from a process-wide environment variable"
    )


def test_gateway_api_key_is_refused_through_purpose_env() -> None:
    """A ``purpose="env"`` reference may not name the gateway credential."""
    environ = {"GATEWAY_API_KEY": "from-the-environment"}

    with pytest.raises(CredentialMissing):
        resolve_for_injection(CredentialRef("env", "GATEWAY_API_KEY"), environ=environ)


def test_gateway_api_key_is_refused_through_purpose_env_even_when_set(
    monkeypatch,
) -> None:
    """Setting the variable in the real environment does not open the seam."""
    monkeypatch.setenv("GATEWAY_API_KEY", "from-the-environment")

    with pytest.raises(CredentialMissing):
        resolve_for_injection(CredentialRef("env", "GATEWAY_API_KEY"))


def test_the_gateway_credential_names_are_not_declared_env_credentials() -> None:
    """Neither allowlist may grow a gateway entry."""
    from app import credentials as creds

    assert "gateway_api_key" not in creds._ENV_CREDENTIAL_NAMES
    assert ("model", "gateway_api_key") not in creds._LEGACY_ENV


def test_a_file_ref_resolves_the_gateway_key(tmp_path) -> None:
    """The file secret provider serves the gateway key."""
    root = tmp_path / "root"
    _secret_file(root, "gateway_api_key", "file-backed-gateway-key")
    environ = {FILE_ROOT_ENV: str(root)}

    value = resolve_for_injection(
        CredentialRef("secret", "file/gateway_api_key"), environ=environ
    )

    assert value == "file-backed-gateway-key"


def test_the_file_provider_reads_any_name_under_the_secret_root(tmp_path) -> None:
    """The file provider is name-driven: any file under the root is a secret."""
    from pipeline.secret_refs import parse_body, read_secret

    root = tmp_path / "root"
    _secret_file(root, "tenant-a/gateway_api_key", "tenant-a-key")
    environ = {FILE_ROOT_ENV: str(root)}

    assert (
        read_secret(parse_body("file/tenant-a/gateway_api_key"), environ=environ)
        == "tenant-a-key"
    )


def test_a_file_ref_is_refused_when_it_escapes_the_secret_root(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    environ = {FILE_ROOT_ENV: str(root)}

    with pytest.raises(CredentialError):
        resolve_for_injection(
            CredentialRef("secret", "file/../outside"), environ=environ
        )


def test_a_missing_file_ref_is_not_configured(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    environ = {FILE_ROOT_ENV: str(root)}

    assert resolve(CredentialRef("secret", "file/absent"), environ=environ) is None


def test_the_gateway_pair_resolves_through_a_file_ref(tmp_path, monkeypatch) -> None:
    """``("model", "gateway_api_key")`` is served by the file provider.

    The credential seam hands the pair to the operator provider, and the
    provider answers it from a ``secret://file/...`` ref - the tenant-scoped
    route. The same pair is refused through ``purpose="env"`` above.
    """
    root = tmp_path / "root"
    _secret_file(root, "gateway_api_key", "file-backed-gateway-key")
    source = textwrap.dedent(
        """
        import app.credentials as creds
        from pipeline.secret_refs import parse_body, read_secret

        class Provider:
            def get(self, ref):
                if ref.purpose != "model":
                    return None
                if ref.name != "gateway_api_key":
                    return None
                raw = read_secret(parse_body("file/gateway_api_key"))
                if raw is None:
                    return None
                return creds.Credential(creds.SecretValue(raw))

        def factory():
            return Provider()
        """
    ).lstrip()
    (tmp_path / "gateway_file_creds_plugin.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(
        "PIPELINE_CREDENTIALS_PLUGIN", "gateway_file_creds_plugin:factory"
    )
    monkeypatch.setenv(FILE_ROOT_ENV, str(root))

    key = resolve_for_injection(CredentialRef("model", "gateway_api_key"))

    assert key == "file-backed-gateway-key"
