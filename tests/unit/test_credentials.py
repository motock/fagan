"""Tests for the credential provider seam (``app.credentials``).

Core only *reads* credentials; the tests below pin the public contract: the
``SecretValue`` redaction guarantee, ``CredentialRef`` validation that never
echoes a value, the open-source ``EnvFileProvider`` default and the
operator-supplied plugin provider, plus ``resolve``'s logging and failure
behaviour.
"""

from __future__ import annotations

import copy
import json
import logging
import pickle
import sys
import textwrap

import pytest

import app.credentials
from app.credentials import (
    Credential,
    CredentialError,
    CredentialRef,
    EnvFileProvider,
    SecretValue,
    credential_provider,
    resolve,
)
from app.plugin_loader import PluginError

SECRET = "sk-test-0001"


@pytest.fixture(autouse=True)
def _isolate_provider_cache():
    """Clear the plugin provider cache around every test."""
    app.credentials._PLUGIN_PROVIDERS.clear()
    yield
    app.credentials._PLUGIN_PROVIDERS.clear()


def _write_plugin(tmp_path, filename: str, source: str) -> str:
    """Write a small plugin module into ``tmp_path`` and return its name.

    Every test uses a unique ``filename``: reusing one would let a later test
    import an earlier test's module from ``sys.modules`` and assert against the
    wrong factory.
    """
    (tmp_path / f"{filename}.py").write_text(
        textwrap.dedent(source).lstrip(), encoding="utf-8"
    )
    return filename


def _credential_records(caplog):
    return [r for r in caplog.records if r.name == "app.credentials"]


# ---------------------------------------------------------------------------
# SecretValue
# ---------------------------------------------------------------------------


def test_secret_value_redacts_in_every_formatting_context():
    value = SecretValue(SECRET)
    # ``str.format`` and percent formatting are exercised through variables so
    # the assertions test the object rather than a literal formatting call.
    brace_template = "{}"
    percent_template = "%s"

    assert str(value) == "<redacted>"
    assert repr(value) == "<redacted>"
    assert brace_template.format(value) == "<redacted>"
    assert f"{value}" == "<redacted>"
    assert percent_template % (value,) == "<redacted>"
    assert format(value, ">20") == "<redacted>"
    assert SECRET not in (str(value) + repr(value) + f"{value}")
    assert bool(value) is True


def test_secret_value_reveal_returns_the_value():
    assert SecretValue(SECRET).reveal() == SECRET


@pytest.mark.parametrize("invalid", ["", 123, None, b"sk-test-0001"])
def test_secret_value_rejects_empty_or_non_string(invalid):
    with pytest.raises(ValueError) as excinfo:
        SecretValue(invalid)

    assert str(invalid) not in str(excinfo.value) or invalid == ""


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(pickle.dumps, id="pickle"),
        pytest.param(copy.copy, id="copy"),
        pytest.param(copy.deepcopy, id="deepcopy"),
    ],
)
def test_secret_value_cannot_be_serialised_or_copied(operation):
    value = SecretValue(SECRET)

    with pytest.raises(TypeError) as excinfo:
        operation(value)

    assert str(excinfo.value) == "SecretValue cannot be serialised or copied"
    assert SECRET not in str(excinfo.value)


def test_secret_value_is_not_json_serialisable():
    value = SecretValue(SECRET)

    with pytest.raises(TypeError) as excinfo:
        json.dumps(value)

    assert SECRET not in str(excinfo.value)


# ---------------------------------------------------------------------------
# CredentialRef
# ---------------------------------------------------------------------------


def test_credential_ref_accepts_a_valid_reference():
    ref = CredentialRef(
        purpose="model",
        name="anthropic_api_key",
        namespace="tenant-a",
        scope=(("region", "eu-west-1"),),
    )

    assert ref.purpose == "model"
    assert ref.name == "anthropic_api_key"
    assert ref.namespace == "tenant-a"
    assert ref.scope == (("region", "eu-west-1"),)


def test_credential_ref_defaults():
    ref = CredentialRef(purpose="model", name="anthropic_api_key")

    assert ref.namespace == "default"
    assert ref.scope == ()


def test_credential_ref_accepts_an_upper_case_env_name():
    ref = CredentialRef(purpose="env", name="MY_TENANT_TOKEN")

    assert ref.name == "MY_TENANT_TOKEN"


@pytest.mark.parametrize(
    ("ref_kwargs", "field", "offending"),
    [
        pytest.param(
            {"purpose": "Model", "name": "api_key"}, "purpose", "Model",
            id="bad-purpose",
        ),
        pytest.param(
            {"purpose": "model", "name": "Api Key"}, "name", "Api Key",
            id="bad-name",
        ),
        pytest.param(
            {"purpose": "model", "name": "api_key", "namespace": "Bad Namespace"},
            "namespace", "Bad Namespace", id="bad-namespace",
        ),
        pytest.param(
            {"purpose": "model", "name": "api_key", "scope": (("Bad Key", "v"),)},
            "scope key", "Bad Key", id="bad-scope-key",
        ),
        pytest.param(
            {"purpose": "model", "name": "api_key", "scope": (("region", "x" * 257),)},
            "scope value", "x" * 257, id="over-long-scope-value",
        ),
        pytest.param(
            {"purpose": "env", "name": "anthropic"}, "name", "anthropic",
            id="lower-case-env-name",
        ),
    ],
)
def test_credential_ref_rejects_invalid_field_without_echoing_the_value(
    ref_kwargs, field, offending
):
    with pytest.raises(CredentialError) as excinfo:
        CredentialRef(**ref_kwargs)

    message = str(excinfo.value)
    assert message == f"invalid credential reference: {field}"
    assert offending not in message


@pytest.mark.parametrize(
    "ref_kwargs",
    [
        pytest.param({"purpose": 123, "name": "api_key"}, id="non-str-purpose"),
        pytest.param({"purpose": "model", "name": 123}, id="non-str-name"),
        pytest.param({"purpose": "model", "name": "api_key", "namespace": None},
                     id="non-str-namespace"),
        pytest.param({"purpose": "model", "name": "api_key", "scope": "region"},
                     id="non-iterable-scope"),
        pytest.param({"purpose": "model", "name": "api_key", "scope": (("region", 5),)},
                     id="non-str-scope-value"),
        pytest.param({"purpose": "model", "name": "api_key", "scope": (("region", "a\x00b"),)},
                     id="control-char-scope-value"),
    ],
)
def test_credential_ref_rejects_wrong_types_as_credential_error(ref_kwargs):
    with pytest.raises(CredentialError):
        CredentialRef(**ref_kwargs)


# ---------------------------------------------------------------------------
# EnvFileProvider
# ---------------------------------------------------------------------------


def test_env_provider_reads_the_named_environment_variable():
    provider = EnvFileProvider({"MY_TENANT_TOKEN": f"  {SECRET}  "})

    credential = provider.get(CredentialRef(purpose="env", name="MY_TENANT_TOKEN"))

    assert isinstance(credential, Credential)
    assert credential.value.reveal() == SECRET


@pytest.mark.parametrize(
    ("purpose", "name", "variable"),
    [
        pytest.param("model", "anthropic_api_key", "PIPELINE_ANTHROPIC_API_KEY",
                     id="anthropic"),
        pytest.param("model", "claude_oauth_token", "CLAUDE_CODE_OAUTH_TOKEN",
                     id="oauth"),
    ],
)
def test_env_provider_reads_the_legacy_model_variables(purpose, name, variable):
    provider = EnvFileProvider({variable: SECRET})

    credential = provider.get(CredentialRef(purpose=purpose, name=name))

    assert credential is not None
    assert credential.value.reveal() == SECRET


@pytest.mark.parametrize(
    ("environ", "ref"),
    [
        pytest.param({"PIPELINE_ANTHROPIC_API_KEY": "   "},
                     CredentialRef("model", "anthropic_api_key"), id="blank"),
        pytest.param({}, CredentialRef("model", "anthropic_api_key"), id="unset"),
        pytest.param({"OTHER_KEY": SECRET}, CredentialRef("model", "unknown_key"),
                     id="unknown-name"),
        pytest.param({}, CredentialRef(purpose="env", name="MY_TENANT_TOKEN"),
                     id="unset-env-variable"),
    ],
)
def test_env_provider_returns_none_when_not_configured(environ, ref):
    assert EnvFileProvider(environ).get(ref) is None


def test_env_provider_ignores_namespace_and_scope():
    provider = EnvFileProvider({"PIPELINE_ANTHROPIC_API_KEY": SECRET})
    ref = CredentialRef(
        purpose="model",
        name="anthropic_api_key",
        namespace="tenant-b",
        scope=(("region", "us-east-1"),),
    )

    credential = provider.get(ref)

    assert credential is not None
    assert credential.value.reveal() == SECRET


def test_env_provider_with_explicit_empty_environ_ignores_ambient(monkeypatch):
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", SECRET)

    assert EnvFileProvider({}).get(CredentialRef("model", "anthropic_api_key")) is None


def test_env_provider_default_environment_is_read_at_call_time(monkeypatch):
    monkeypatch.delenv("PIPELINE_ANTHROPIC_API_KEY", raising=False)
    provider = EnvFileProvider()

    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", SECRET)

    credential = provider.get(CredentialRef("model", "anthropic_api_key"))
    assert credential is not None
    assert credential.value.reveal() == SECRET


# ---------------------------------------------------------------------------
# credential_provider
# ---------------------------------------------------------------------------


def test_credential_provider_defaults_to_env_file_provider():
    provider = credential_provider({})

    assert isinstance(provider, EnvFileProvider)


def test_credential_provider_uses_the_plugin_provider_and_caches_it(
    tmp_path, monkeypatch
):
    name = _write_plugin(
        tmp_path,
        "creds_plugin_cache",
        """
        import app.credentials as creds

        creations = []

        class Provider:
            def get(self, ref):
                return None

        def factory():
            creations.append(Provider())
            return creations[-1]
        """,
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    environ = {"PIPELINE_CREDENTIALS_PLUGIN": f"{name}:factory"}

    first = credential_provider(environ)
    second = credential_provider(environ)

    assert first is second
    assert len(sys.modules[name].creations) == 1


def test_env_provider_is_not_used_when_a_plugin_is_configured(tmp_path, monkeypatch):
    name = _write_plugin(
        tmp_path,
        "creds_plugin_not_env",
        """
        class Provider:
            def get(self, ref):
                return None

        def factory():
            return Provider()
        """,
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    provider = credential_provider(
        {"PIPELINE_CREDENTIALS_PLUGIN": f"{name}:factory"}
    )

    assert not isinstance(provider, EnvFileProvider)


def test_credential_provider_cache_is_keyed_by_spec(tmp_path, monkeypatch):
    first_name = _write_plugin(
        tmp_path,
        "creds_plugin_spec_a",
        """
        creations = []

        class Provider:
            def get(self, ref):
                return None

        def factory():
            creations.append(Provider())
            return creations[-1]
        """,
    )
    second_name = _write_plugin(
        tmp_path,
        "creds_plugin_spec_b",
        """
        class Provider:
            def get(self, ref):
                return None

        def factory():
            return Provider()
        """,
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    first_environ = {"PIPELINE_CREDENTIALS_PLUGIN": f"{first_name}:factory"}
    second_environ = {"PIPELINE_CREDENTIALS_PLUGIN": f"{second_name}:factory"}

    first = credential_provider(first_environ)
    second = credential_provider(second_environ)
    first_again = credential_provider(first_environ)

    assert first is not second
    assert first_again is first
    assert len(sys.modules[first_name].creations) == 1


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param("not a spec", id="malformed"),
        pytest.param("no_such_module_xyz:factory", id="missing-module"),
        pytest.param("json:loads", id="stdlib-module"),
    ],
)
def test_credential_provider_rejects_a_bad_spec_without_fallback(spec):
    with pytest.raises(PluginError):
        credential_provider({"PIPELINE_CREDENTIALS_PLUGIN": spec})


def test_credential_provider_rejects_a_provider_without_get(tmp_path, monkeypatch):
    name = _write_plugin(
        tmp_path,
        "creds_plugin_no_get",
        """
        def factory():
            return object()
        """,
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    environ = {"PIPELINE_CREDENTIALS_PLUGIN": f"{name}:factory"}

    with pytest.raises(PluginError) as excinfo:
        credential_provider(environ)

    assert "get" in str(excinfo.value)
    assert app.credentials._PLUGIN_PROVIDERS == {}

    # A rejected provider must not be cached: the second call fails again.
    with pytest.raises(PluginError) as excinfo_again:
        credential_provider(environ)

    assert "get" in str(excinfo_again.value)


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def _install_provider_plugin(tmp_path, monkeypatch, filename: str, body: str) -> dict:
    name = _write_plugin(tmp_path, filename, body)
    monkeypatch.syspath_prepend(str(tmp_path))
    return {"PIPELINE_CREDENTIALS_PLUGIN": f"{name}:factory"}


def test_resolve_returns_a_secret_value_and_logs_one_info_record(
    tmp_path, monkeypatch, caplog
):
    environ = _install_provider_plugin(
        tmp_path,
        monkeypatch,
        "creds_plugin_resolve_ok",
        """
        import app.credentials as creds

        class Provider:
            def get(self, ref):
                return creds.Credential(creds.SecretValue('sk-test-0001'))

        def factory():
            return Provider()
        """,
    )
    ref = CredentialRef(purpose="model", name="anthropic_api_key")

    with caplog.at_level(logging.INFO, logger="app.credentials"):
        resolved = resolve(ref, environ=environ)

    assert isinstance(resolved, SecretValue)
    assert resolved.reveal() == SECRET
    records = _credential_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.INFO
    assert record.credential_outcome == "ok"
    assert record.credential_purpose == "model"
    assert record.credential_name == "anthropic_api_key"
    assert record.credential_namespace == "default"
    assert SECRET not in caplog.text


def test_resolve_logs_the_env_variable_name_but_never_its_value(caplog):
    ref = CredentialRef(purpose="env", name="MY_TENANT_TOKEN")

    with caplog.at_level(logging.INFO, logger="app.credentials"):
        resolved = resolve(ref, environ={"MY_TENANT_TOKEN": SECRET})

    assert resolved is not None
    assert resolved.reveal() == SECRET
    records = _credential_records(caplog)
    assert len(records) == 1
    assert records[0].credential_name == "MY_TENANT_TOKEN"
    assert records[0].credential_outcome == "ok"
    assert SECRET not in caplog.text


def test_resolve_returns_none_and_logs_unset_when_not_configured(caplog):
    ref = CredentialRef(purpose="model", name="anthropic_api_key")

    with caplog.at_level(logging.INFO, logger="app.credentials"):
        assert resolve(ref, environ={}) is None

    records = _credential_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.INFO
    assert record.credential_outcome == "unset"
    assert record.credential_purpose == "model"
    assert record.credential_name == "anthropic_api_key"
    assert record.credential_namespace == "default"


def test_resolve_wraps_an_unexpected_provider_failure_without_leaking(
    tmp_path, monkeypatch, caplog
):
    environ = _install_provider_plugin(
        tmp_path,
        monkeypatch,
        "creds_plugin_boom",
        """
        class Provider:
            def get(self, ref):
                raise RuntimeError('boom-secret')

        def factory():
            return Provider()
        """,
    )
    ref = CredentialRef(purpose="model", name="anthropic_api_key")

    with (
        caplog.at_level(logging.INFO, logger="app.credentials"),
        pytest.raises(CredentialError) as excinfo,
    ):
        resolve(ref, environ=environ)

    assert str(excinfo.value) == "credential provider failed"
    assert "boom-secret" not in str(excinfo.value)
    assert "boom-secret" not in caplog.text
    records = _credential_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.credential_outcome == "error"
    assert record.error_type == "RuntimeError"
    assert record.credential_name == "anthropic_api_key"


def test_resolve_propagates_a_credential_error_unchanged(
    tmp_path, monkeypatch, caplog
):
    environ = _install_provider_plugin(
        tmp_path,
        monkeypatch,
        "creds_plugin_credential_error",
        """
        import app.credentials as creds

        class Provider:
            def get(self, ref):
                raise creds.CredentialError('provider unavailable')

        def factory():
            return Provider()
        """,
    )
    ref = CredentialRef(purpose="model", name="anthropic_api_key")

    with (
        caplog.at_level(logging.INFO, logger="app.credentials"),
        pytest.raises(CredentialError) as excinfo,
    ):
        resolve(ref, environ=environ)

    assert str(excinfo.value) == "provider unavailable"
    records = _credential_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].credential_outcome == "error"


def test_resolve_rejects_a_non_credential_result(tmp_path, monkeypatch, caplog):
    environ = _install_provider_plugin(
        tmp_path,
        monkeypatch,
        "creds_plugin_invalid_result",
        """
        class Provider:
            def get(self, ref):
                return 'sk-test-0001'

        def factory():
            return Provider()
        """,
    )
    ref = CredentialRef(purpose="model", name="anthropic_api_key")

    with (
        caplog.at_level(logging.INFO, logger="app.credentials"),
        pytest.raises(CredentialError) as excinfo,
    ):
        resolve(ref, environ=environ)

    assert str(excinfo.value) == "credential provider returned an invalid result"
    assert SECRET not in str(excinfo.value)
    assert SECRET not in caplog.text
    records = _credential_records(caplog)
    assert len(records) == 1
    assert records[0].credential_outcome == "invalid"


def test_resolve_accepts_a_credential_expiring_in_the_future(
    tmp_path, monkeypatch, caplog
):
    environ = _install_provider_plugin(
        tmp_path,
        monkeypatch,
        "creds_plugin_not_expired",
        """
        import app.credentials as creds

        class Provider:
            def get(self, ref):
                return creds.Credential(
                    creds.SecretValue('sk-test-0001'), expires_at=1000.0
                )

        def factory():
            return Provider()
        """,
    )
    ref = CredentialRef(purpose="model", name="anthropic_api_key")

    with caplog.at_level(logging.INFO, logger="app.credentials"):
        resolved = resolve(ref, environ=environ, now=999.999)

    assert resolved is not None
    assert resolved.reveal() == SECRET
    assert _credential_records(caplog)[0].credential_outcome == "ok"


def test_resolve_rejects_a_credential_at_the_expiry_boundary(
    tmp_path, monkeypatch, caplog
):
    environ = _install_provider_plugin(
        tmp_path,
        monkeypatch,
        "creds_plugin_expired",
        """
        import app.credentials as creds

        class Provider:
            def get(self, ref):
                return creds.Credential(
                    creds.SecretValue('sk-test-0001'), expires_at=1000.0
                )

        def factory():
            return Provider()
        """,
    )
    ref = CredentialRef(purpose="model", name="anthropic_api_key")

    with (
        caplog.at_level(logging.INFO, logger="app.credentials"),
        pytest.raises(CredentialError) as excinfo,
    ):
        resolve(ref, environ=environ, now=1000.0)

    assert str(excinfo.value) == "credential expired"
    assert SECRET not in str(excinfo.value)
    assert SECRET not in caplog.text
    records = _credential_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].credential_outcome == "expired"


def test_resolve_treats_a_falsy_now_as_an_explicit_clock(tmp_path, monkeypatch):
    environ = _install_provider_plugin(
        tmp_path,
        monkeypatch,
        "creds_plugin_falsy_now",
        """
        import app.credentials as creds

        class Provider:
            def get(self, ref):
                return creds.Credential(
                    creds.SecretValue('sk-test-0001'), expires_at=5.0
                )

        def factory():
            return Provider()
        """,
    )
    ref = CredentialRef(purpose="model", name="anthropic_api_key")

    resolved = resolve(ref, environ=environ, now=0.0)

    assert resolved is not None
    assert resolved.reveal() == SECRET


def test_resolve_does_not_memoise_a_resolved_credential(tmp_path, monkeypatch):
    environ = _install_provider_plugin(
        tmp_path,
        monkeypatch,
        "creds_plugin_no_memo",
        """
        import app.credentials as creds

        class Provider:
            def get(self, ref):
                return creds.Credential(
                    creds.SecretValue('sk-test-0001'), expires_at=1000.0
                )

        def factory():
            return Provider()
        """,
    )
    ref = CredentialRef(purpose="model", name="anthropic_api_key")

    assert resolve(ref, environ=environ, now=999.0) is not None
    with pytest.raises(CredentialError) as excinfo:
        resolve(ref, environ=environ, now=2000.0)

    assert str(excinfo.value) == "credential expired"


def test_resolve_propagates_a_plugin_error_without_environment_fallback(
    monkeypatch, caplog
):
    monkeypatch.setenv("PIPELINE_ANTHROPIC_API_KEY", SECRET)
    ref = CredentialRef(purpose="model", name="anthropic_api_key")

    with (
        caplog.at_level(logging.INFO, logger="app.credentials"),
        pytest.raises(PluginError) as excinfo,
    ):
        resolve(ref, environ={"PIPELINE_CREDENTIALS_PLUGIN": "not a spec"})

    assert SECRET not in str(excinfo.value)
    assert SECRET not in caplog.text
