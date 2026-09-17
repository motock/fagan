"""Regression test for server.json description length.

This test intentionally fails until the implementation file `server.json`
contains a description that satisfies the MCP Registry schema's
``maxLength: 100`` constraint. The original manifest shipped a 141‑character
description, which caused registry validation to reject the file.

The test asserts that the length of the description field is at most 100
characters. Until the implementation is fixed, this assertion will raise an
``AssertionError``.
"""

import json
import pytest
from pathlib import Path

# Load the manifest
SERVER_JSON = Path("server.json")


def test_description_length_is_within_schema_limit():
    """The description must not exceed 100 characters.

    The original manifest had a 141‑character description, which violated the
    MCP Registry schema ``maxLength: 100``. This test will fail with an
    ``AssertionError`` until the implementation is corrected.
    """
    data = json.load(SERVER_JSON.open("r", encoding="utf-8"))
    description = data.get("description", "")
    assert isinstance(description, str), "description must be a string"
    assert len(description) <= 100, (
        f"description length {len(description)} exceeds the schema's maxLength 100"
    )
