"""Tests for ``secret://provider/name`` resolution (``pipeline.secret_refs``).

A secret reference names a provider and a name inside it. The grammar, the
built-in ``file`` and ``env`` providers, the operator plugin seam and the
redaction guarantees are pinned here. ``app.credentials.resolve`` routes a
``purpose="secret"`` reference through this module; the legacy path is pinned
too, so an unconfigured deployment still resolves exactly as before.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import textwrap

import pytest

from app.credentials import (
    Credential,
    CredentialError,
    CredentialRef,
    SecretValue,
    resolve,
)
from pipeline.actor import bind_namespace
from pipeline.secret_refs import SecretRef, SecretRefError, parse_ref
from pipeline.store_records import DEFAULT_NAMESPACE

FILE_ROOT_ENV = "PIPELINE_SECRET_FILE_ROOT"
PLUGIN_ENV = "PIPELINE_SECRET_PROVIDER_PLUGINS"


# --- parse_ref: grammar positives -------------------------------------------


@pytest.mark.parametrize(
    ("text", "provider", "name"),
    [
        (
            "secret://env/PIPELINE_ANTHROPIC_API_KEY",
            "env",
            "PIPELINE_ANTHROPIC_API_KEY",
        ),
        ("secret://file/db.password", "file", "db.password"),
        ("secret://file/prod/db-pass_1.txt", "file", "prod/db-pass_1.txt"),
        ("secret://vault_2/a", "vault_2", "a"),
        ("secret://a/" + "n" * 128, "a", "n" * 128),
        ("secret://" + "p" * 32 + "/x", "p" * 32, "x"),
    ],
)
def test_parse_ref_accepts_a_well_formed_reference(text, provider, name):
    assert parse_ref(text) == SecretRef(provider=provider, name=name)


# --- parse_ref: grammar negatives -------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "file/db.password",  # no scheme
        "http://file/db.password",  # wrong scheme
        "secret://",  # empty body
        "secret://file",  # no name
        "secret://file/",  # empty name
        "secret://File/x",  # upper-case provider
        "secret://1file/x",  # provider starts with a digit
        "secret://" + "p" * 33 + "/x",  # provider too long
        "secret://file/" + "n" * 129,  # name too long
        "secret://file/../outside",  # traversal
        "secret://file/a/../b",  # traversal mid-name
        "secret://file//etc/passwd",  # absolute name
        "secret://file/a b",  # whitespace
        "secret://file/a$b",  # shell metacharacter
        "secret://file/a\n",  # trailing newline is not matched
        "secret://file/a\\b",  # backslash
    ],
)
def test_parse_ref_rejects_a_malformed_reference(text):
    with pytest.raises(SecretRefError):
        parse_ref(text)


@pytest.mark.parametrize("text", [None, 42, b"secret://file/x"])
def test_parse_ref_rejects_a_non_string(text):
    with pytest.raises(SecretRefError):
        parse_ref(text)


def test_parse_ref_error_never_echoes_the_reference():
    with pytest.raises(SecretRefError) as info:
        parse_ref("secret://file/../hunter2-password")

    assert "hunter2-password" not in str(info.value)


# --- CredentialRef: the secret purpose is validated at construction ---------


@pytest.mark.parametrize(
    "name",
    ["no-slash", "file/../x", "file//abs", "Bad/x", "file/"],
)
def test_credential_ref_rejects_a_malformed_secret_name(name):
    with pytest.raises(CredentialError):
        CredentialRef(purpose="secret", name=name)


# --- file provider ----------------------------------------------------------


def _secret_file(root, relative, value, mode=0o600):
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_file_provider_reads_a_private_file_under_the_root(tmp_path):
    root = tmp_path / "root"
    _secret_file(root, "db/pass", "s3cret-value")

    value = resolve(
        CredentialRef(purpose="secret", name="file/db/pass"),
        environ={FILE_ROOT_ENV: str(root)},
    )

    assert value.reveal() == "s3cret-value"


def test_file_provider_returns_none_for_a_missing_file(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    value = resolve(
        CredentialRef(purpose="secret", name="file/absent"),
        environ={FILE_ROOT_ENV: str(root)},
    )

    assert value is None


def test_file_provider_returns_none_for_an_empty_file(tmp_path):
    root = tmp_path / "root"
    path = _secret_file(root, "empty", "")

    assert path.read_text() == "\n"
    assert (
        resolve(
            CredentialRef(purpose="secret", name="file/empty"),
            environ={FILE_ROOT_ENV: str(root)},
        )
        is None
    )


def test_file_provider_refuses_when_no_root_is_configured(tmp_path):
    with pytest.raises(CredentialError, match="not configured"):
        resolve(CredentialRef(purpose="secret", name="file/x"), environ={})


def test_file_provider_refuses_a_root_that_is_not_a_directory(tmp_path):
    not_a_dir = tmp_path / "file-not-dir"
    not_a_dir.write_text("x", encoding="utf-8")

    with pytest.raises(CredentialError):
        resolve(
            CredentialRef(purpose="secret", name="file/x"),
            environ={FILE_ROOT_ENV: str(not_a_dir)},
        )


def test_file_provider_refuses_a_symlink_that_escapes_the_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside-secret"
    outside.write_text("escaped-value\n", encoding="utf-8")
    os.chmod(outside, 0o600)
    (root / "link").symlink_to(outside)

    with pytest.raises(CredentialError) as info:
        resolve(
            CredentialRef(purpose="secret", name="file/link"),
            environ={FILE_ROOT_ENV: str(root)},
        )

    assert "escaped-value" not in str(info.value)


def test_file_provider_refuses_a_directory_directly_under_the_root(tmp_path):
    root = tmp_path / "root"
    (root / "dir").mkdir(parents=True)

    with pytest.raises(CredentialError):
        resolve(
            CredentialRef(purpose="secret", name="file/dir"),
            environ={FILE_ROOT_ENV: str(root)},
        )


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o620])
def test_file_provider_refuses_a_group_or_world_accessible_file(tmp_path, mode):
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits")
    root = tmp_path / "root"
    _secret_file(root, "loose", "value", mode=mode)

    with pytest.raises(CredentialError) as info:
        resolve(
            CredentialRef(purpose="secret", name="file/loose"),
            environ={FILE_ROOT_ENV: str(root)},
        )

    assert "value" not in str(info.value)
    assert stat.S_IMODE(os.stat(root / "loose").st_mode) == mode


# --- env provider -----------------------------------------------------------


def test_env_provider_reads_the_variable_in_the_default_namespace():
    value = resolve(
        CredentialRef(purpose="secret", name="env/PIPELINE_ANTHROPIC_API_KEY"),
        environ={"PIPELINE_ANTHROPIC_API_KEY": "env-value"},
    )

    assert value.reveal() == "env-value"


def test_env_provider_default_namespace_matches_the_store_default():
    with bind_namespace(DEFAULT_NAMESPACE):
        value = resolve(
            CredentialRef(purpose="secret", name="env/SOME_VAR"),
            environ={"SOME_VAR": "v"},
        )

    assert value.reveal() == "v"


def test_env_provider_returns_none_for_an_unset_variable():
    assert (
        resolve(
            CredentialRef(purpose="secret", name="env/UNSET_VAR"),
            environ={},
        )
        is None
    )


def test_env_provider_refuses_outside_the_default_namespace():
    with (
        bind_namespace("other/prod"),
        pytest.raises(CredentialError, match="namespace"),
    ):
        resolve(
            CredentialRef(purpose="secret", name="env/PIPELINE_ANTHROPIC_API_KEY"),
            environ={"PIPELINE_ANTHROPIC_API_KEY": "must-not-leak"},
        )


# --- unknown providers ------------------------------------------------------


def test_unknown_provider_is_refused_without_a_plugin():
    with pytest.raises(CredentialError, match="unknown secret provider"):
        resolve(CredentialRef(purpose="secret", name="vault/db"), environ={})


# --- operator plugin providers ----------------------------------------------


def _write_provider_module(tmp_path, module, body):
    (tmp_path / f"{module}.py").write_text(textwrap.dedent(body), encoding="utf-8")


def test_plugin_provider_resolves_its_own_provider_name(tmp_path, monkeypatch):
    module = "secret_plugin_resolves"
    _write_provider_module(
        tmp_path,
        module,
        """
        from app.credentials import Credential, SecretValue

        class Provider:
            name = "vault"

            def get(self, name):
                if name == "db":
                    return "plugin-value"
                return None

        def build():
            return Provider()
        """,
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    environ = {PLUGIN_ENV: f"{module}:build"}

    value = resolve(CredentialRef(purpose="secret", name="vault/db"), environ=environ)
    unknown = resolve(
        CredentialRef(purpose="secret", name="vault/other"), environ=environ
    )

    assert value.reveal() == "plugin-value"
    assert unknown is None


def test_plugin_cannot_claim_a_builtin_provider_name(tmp_path, monkeypatch):
    module = "secret_plugin_claims_file"
    _write_provider_module(
        tmp_path,
        module,
        """
        class Provider:
            name = "file"

            def get(self, name):
                return None

        def build():
            return Provider()
        """,
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(CredentialError):
        resolve(
            CredentialRef(purpose="secret", name="file/x"),
            environ={PLUGIN_ENV: f"{module}:build"},
        )


def test_plugin_without_a_get_method_is_refused(tmp_path, monkeypatch):
    module = "secret_plugin_no_get"
    _write_provider_module(
        tmp_path,
        module,
        """
        class Provider:
            name = "vault"

        def build():
            return Provider()
        """,
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(CredentialError):
        resolve(
            CredentialRef(purpose="secret", name="vault/db"),
            environ={PLUGIN_ENV: f"{module}:build"},
        )


def test_plugin_with_an_invalid_provider_name_is_refused(tmp_path, monkeypatch):
    module = "secret_plugin_bad_name"
    _write_provider_module(
        tmp_path,
        module,
        """
        class Provider:
            name = "Not/Valid"

            def get(self, name):
                return None

        def build():
            return Provider()
        """,
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(CredentialError):
        resolve(
            CredentialRef(purpose="secret", name="vault/db"),
            environ={PLUGIN_ENV: f"{module}:build"},
        )


# --- redaction and logging --------------------------------------------------


def test_credential_repr_and_str_redact_the_value():
    credential = Credential(SecretValue("hunter2-value"))

    assert "hunter2-value" not in repr(credential)
    assert "hunter2-value" not in str(credential)
    assert "hunter2-value" not in f"{credential}"


def test_resolved_secret_value_redacts_when_printed(tmp_path):
    root = tmp_path / "root"
    _secret_file(root, "pw", "hunter2-value")

    value = resolve(
        CredentialRef(purpose="secret", name="file/pw"),
        environ={FILE_ROOT_ENV: str(root)},
    )

    assert "hunter2-value" not in repr(value)
    assert "hunter2-value" not in str(value)


def test_secret_resolution_logs_no_identifier_or_value(tmp_path, caplog):
    root = tmp_path / "root"
    _secret_file(root, "identifying/name", "hunter2-value")

    with caplog.at_level(logging.DEBUG, logger="app.credentials"):
        resolve(
            CredentialRef(purpose="secret", name="file/identifying/name"),
            environ={FILE_ROOT_ENV: str(root)},
        )

    assert caplog.records
    rendered = " ".join(
        record.getMessage() + repr(record.__dict__) for record in caplog.records
    )
    assert "identifying/name" not in rendered
    assert "hunter2-value" not in rendered


# --- the unchanged legacy path ----------------------------------------------


def test_legacy_reference_is_unaffected_by_secret_configuration(tmp_path):
    root = tmp_path / "root"
    _secret_file(root, "pw", "file-value")
    environ = {
        "PIPELINE_ANTHROPIC_API_KEY": "legacy-value",
        FILE_ROOT_ENV: str(root),
    }

    value = resolve(
        CredentialRef(purpose="env", name="PIPELINE_ANTHROPIC_API_KEY"), environ=environ
    )

    assert value.reveal() == "legacy-value"
    assert isinstance(value, SecretValue)
