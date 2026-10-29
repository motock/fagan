"""TDD tests for ``pipeline.parsers._validate_key``.

plan_name and story_key flow into filesystem paths (see
``pipeline.review_job``), so this boundary check must reject anything that
could traverse a path.  "["A-Za-z0-9._-]" allows "." and "..", which are
path traversal, not keys: a story key of ".." resolves the job directory to the
state root itself.  Dot-only names are therefore rejected; a dot *inside* a
name (e.g. a version) stays valid.
"""

from __future__ import annotations

import pytest

from pipeline.parsers import _validate_key


@pytest.mark.parametrize("name", [".", "..", "..."])
def test_dot_only_names_are_rejected(name):
    with pytest.raises(ValueError):
        _validate_key(name)


@pytest.mark.parametrize("name", ["SQ-01", "a_b", "v1.2.3", "a..b"])
def test_ordinary_keys_are_accepted(name):
    _validate_key(name)  # must not raise
