"""PIPELINE_DASHBOARD_ALLOWED_HOSTS: operator allowlist for proxied hostnames."""
from __future__ import annotations

import logging

import pytest

from app.auth import host_is_allowed

ENV = "PIPELINE_DASHBOARD_ALLOWED_HOSTS"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


def test_unset_refuses_public_hostname():
    assert host_is_allowed("pipeline.example.com", "0.0.0.0") is False


def test_unset_still_accepts_loopback():
    assert host_is_allowed("localhost:8000", None) is True


def test_blank_refuses_public_hostname(monkeypatch):
    monkeypatch.setenv(ENV, "  ")
    assert host_is_allowed("pipeline.example.com", "0.0.0.0") is False


def test_listed_hostname_with_port_is_accepted(monkeypatch):
    monkeypatch.setenv(ENV, "pipeline.example.com")
    assert host_is_allowed("pipeline.example.com:8443", "0.0.0.0") is True


def test_listed_hostname_matches_case_insensitively(monkeypatch):
    monkeypatch.setenv(ENV, "pipeline.example.com")
    assert host_is_allowed("PIPELINE.example.com", "0.0.0.0") is True


def test_uppercase_entry_matches_lowercase_host(monkeypatch):
    monkeypatch.setenv(ENV, " Pipeline.Example.COM ")
    assert host_is_allowed("pipeline.example.com", "0.0.0.0") is True


def test_unlisted_hostname_is_refused(monkeypatch):
    monkeypatch.setenv(ENV, "pipeline.example.com")
    assert host_is_allowed("evil.example.com", "0.0.0.0") is False


def test_listed_hostname_does_not_accept_subdomain(monkeypatch):
    monkeypatch.setenv(ENV, "example.com")
    assert host_is_allowed("evil.example.com", "0.0.0.0") is False


def test_wildcard_entry_is_ignored(monkeypatch):
    monkeypatch.setenv(ENV, "*.example.com")
    assert host_is_allowed("a.example.com", "0.0.0.0") is False


def test_bare_star_entry_is_ignored(monkeypatch):
    monkeypatch.setenv(ENV, "*")
    assert host_is_allowed("a.example.com", "0.0.0.0") is False


def test_only_valid_entry_of_mixed_list_is_accepted(monkeypatch):
    monkeypatch.setenv(ENV, "https://x.example.com,x.example.com:8443, ,ok.example.com")
    assert host_is_allowed("ok.example.com", "0.0.0.0") is True


def test_entry_with_scheme_is_ignored(monkeypatch):
    monkeypatch.setenv(ENV, "https://x.example.com,x.example.com:8443, ,ok.example.com")
    assert host_is_allowed("x.example.com", "0.0.0.0") is False


def test_entry_with_path_is_ignored(monkeypatch):
    monkeypatch.setenv(ENV, "x.example.com/admin")
    assert host_is_allowed("x.example.com", "0.0.0.0") is False


def test_entry_with_inner_whitespace_is_ignored(monkeypatch):
    monkeypatch.setenv(ENV, "x example.com")
    assert host_is_allowed("x", "0.0.0.0") is False


def test_ip_entry_is_accepted(monkeypatch):
    monkeypatch.setenv(ENV, "10.0.0.5")
    assert host_is_allowed("10.0.0.5:8000", "0.0.0.0") is True


def test_empty_host_header_is_refused(monkeypatch):
    monkeypatch.setenv(ENV, "ok.example.com")
    assert host_is_allowed("", "0.0.0.0") is False


def test_none_host_header_is_refused(monkeypatch):
    monkeypatch.setenv(ENV, "ok.example.com")
    assert host_is_allowed(None, "0.0.0.0") is False


def test_invalid_entry_logs_warning_naming_it(monkeypatch, caplog):
    monkeypatch.setenv(ENV, "*.warn-once.example.com")
    with caplog.at_level(logging.WARNING, logger="app.auth"):
        host_is_allowed("a.example.com", "0.0.0.0")
    assert [r.entry for r in caplog.records] == ["*.warn-once.example.com"]


def test_invalid_entry_warns_only_once(monkeypatch, caplog):
    monkeypatch.setenv(ENV, "https://warn-twice.example.com")
    with caplog.at_level(logging.WARNING, logger="app.auth"):
        host_is_allowed("a.example.com", "0.0.0.0")
        host_is_allowed("a.example.com", "0.0.0.0")
    assert [r.entry for r in caplog.records] == ["https://warn-twice.example.com"]
