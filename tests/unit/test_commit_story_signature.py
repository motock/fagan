"""Signature pin for ``commit_story``: plan, story key and story - nothing else.

``commit_story`` re-reads and writes the manifest through the record store, so
the ``manifest_path`` it used to take is dead weight: a caller could pass any
path and the store would ignore it and write its own ``<plan>.manifest.json``
anyway. A path argument that is silently discarded is worse than no argument -
it reads like an interceptor. This pin keeps it gone.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

# pipeline.server is imported explicitly and first: importing any pipeline
# submodule before it trips the pre-existing package import cycle.
import pipeline.server  # noqa: F401
from pipeline import story_commit


def test_should_accept_plan_story_key_and_story_only():
    assert list(inspect.signature(story_commit.commit_story).parameters) == [
        "plan_name",
        "story_key",
        "story",
    ]


def test_should_reject_a_fourth_positional_argument():
    with pytest.raises(TypeError):
        story_commit.commit_story(
            "plan-a",
            "story-1",
            {"state": "ready"},
            Path("/tmp/whatever.manifest.json"),
        )
