"""Which strings can name the repo of a team_repo settings record."""

from __future__ import annotations

import pytest

from pipeline.store_settings import REPO_MAX_LENGTH, is_settings_repo


def test_a_host_org_name_is_a_settings_repo():
    assert is_settings_repo("github.com/motock/fagan") is True


@pytest.mark.parametrize(
    "repo",
    [
        "/var/lib/pipeline/repos/github.com/motock/fagan",
        "",
        "acme/core",
        "github.com/acme/core\n",
        "github.com/acme/" + "c" * REPO_MAX_LENGTH,
        None,
        42,
    ],
)
def test_anything_else_is_not_a_settings_repo(repo):
    assert is_settings_repo(repo) is False
