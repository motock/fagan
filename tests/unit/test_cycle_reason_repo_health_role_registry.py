"""Guards the corrected import-cycle rationale in repo_health / role_registry."""

import inspect

from app import role_registry
from pipeline import repo_health


def test_ci_finding_docstring_drops_the_false_cycle_claim():
    doc = repo_health.ci_finding.__doc__ or ""
    assert "to avoid circular imports" not in doc
    assert "pipeline.ci" in doc
    assert "would cycle" in doc


def test_role_registry_comment_no_longer_blames_pipeline_config():
    src = inspect.getsource(role_registry)
    assert "importing pipeline back from here would create a circular import" not in src
    assert "deliberately imports nothing from" in src
