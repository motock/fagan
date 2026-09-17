import json
from pathlib import Path

# This test intentionally fails until the implementation file `server.json`
# contains a description that satisfies the MCP Registry schema's
# ``maxLength: 100`` constraint. The original manifest shipped a 141‑character
# description, which caused registry validation to reject the file.

def test_description_length_is_within_schema_limit():
    """The description must not exceed 100 characters.

    The original manifest had a 141‑character description, which violated the
    MCP Registry schema ``maxLength: 100``. This test will fail with an
    ``AssertionError`` until the implementation is corrected.
    """
    data = json.load(Path("server.json").open("r", encoding="utf-8"))
    description = data.get("description", "")
    assert isinstance(description, str), "description must be a string"
    assert len(description) <= 100, (
        f"description length {len(description)} exceeds the schema's maxLength 100"
    )
