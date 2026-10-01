"""Redaction rules for the scheduler's env-conflict warning and _is_secret."""
import pytest

from pipeline.config_provenance import _is_secret
from tests.unit.test_scheduler_env_conflict_report import (
    _point_at_fixtures,
    _stderr_lines,
)


def _report():
    import pipeline.scheduler_daemon as mod

    mod._report_env_conflicts()


def test_should_print_both_values_for_differing_pipeline_autonomy(
    tmp_path, monkeypatch, capsys
):
    _point_at_fixtures(
        monkeypatch, tmp_path, {"PIPELINE_AUTONOMY": "high"}, {"PIPELINE_AUTONOMY": "low"}
    )
    _report()
    (line,) = _stderr_lines(capsys)
    assert "launchd_plist=high mcp_server_env=low" in line


@pytest.mark.parametrize(
    "name",
    [
        "DATABASE_URL",
        "SMTP_PASSWD",
        "api_key",
        "SERVICE_AUTH",
        "PRIVATE_CERT",
        "SENTRY_DSN",
        "SOME_PLAIN_SETTING",
    ],
)
def test_should_not_echo_value_of_non_printable_name(
    name, tmp_path, monkeypatch, capsys
):
    _point_at_fixtures(
        monkeypatch, tmp_path, {name: "plist-s3cret-literal"}, {name: "mcp-s3cret-literal"}
    )
    _report()
    err = capsys.readouterr().err
    assert f"WARNING {name} differs" in err
    assert "s3cret-literal" not in err


@pytest.mark.parametrize(
    "name", ["PIPELINE_BACKEND_TEST_AUTHOR", "PIPELINE_TEST_AUTHOR_TIMEOUT_SECONDS"]
)
def test_should_not_treat_author_substring_as_secret(name):
    assert _is_secret(name) is False


@pytest.mark.parametrize(
    "name",
    ["SERVICE_AUTH", "AUTHORIZATION", "SMTP_PASSWD", "PRIVATE_CERT", "SENTRY_DSN", "api_key", "db_Token"],
)
def test_should_treat_token_and_case_variants_as_secret(name):
    assert _is_secret(name) is True


@pytest.mark.parametrize("name", ["PIPELINE_AUTONOMY", "DSNX", "PRIVATELY_SET"])
def test_should_not_treat_partial_token_as_secret(name):
    assert _is_secret(name) is False
