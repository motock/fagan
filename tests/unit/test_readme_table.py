"""Regression test: the "Components at a glance" table in README.md must be a
single unbroken markdown table (no blank lines between the header separator
row and the last data row).

A blank line inside a CommonMark table terminates the table, so any rows
after the blank line render as loose pipe-delimited text instead of table
rows. This test guards against that regression.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
README = REPO_ROOT / "README.md"


def test_components_table_is_unbroken():
    assert README.is_file(), "README.md must exist at the repo root"
    text = README.read_text()
    lines = text.splitlines()

    # Locate the "## Components at a glance" heading.
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "## Components at a glance":
            start = i
            break
    assert start is not None, "## Components at a glance section not found in README.md"

    # Collect the table block: consecutive non-blank lines that start with '|'.
    table_rows: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() == "":
            # A blank line ends the table block.
            if table_rows:
                break
            continue
        if line.lstrip().startswith("|"):
            table_rows.append(line)
        else:
            break

    assert len(table_rows) >= 2, (
        f"Expected a table with a header + separator + data rows, "
        f"got {len(table_rows)} rows: {table_rows}"
    )

    # The table must end with the "Issue tracker" row (the last data row).
    last_row = table_rows[-1]
    assert "Issue tracker" in last_row, (
        f"Expected the table to end with the 'Issue tracker' row, "
        f"got last row: {last_row!r}"
    )

    # No blank line should appear between the header separator row and the
    # last data row — i.e. every line from the first '|' row through the last
    # '|' row must be a pipe row with no blank lines in between.
    # We already collected consecutive non-blank pipe rows, so verify there
    # are no gaps by re-scanning the original text between the first and last
    # table row.
    first_table_line = table_rows[0]
    last_table_line = table_rows[-1]
    first_idx = None
    last_idx = None
    for i, line in enumerate(lines):
        if line is first_table_line and first_idx is None:
            first_idx = i
        if line is last_table_line:
            last_idx = i
    assert first_idx is not None and last_idx is not None
    for i in range(first_idx, last_idx + 1):
        assert lines[i].lstrip().startswith("|"), (
            f"Blank line or non-table line at README.md line {i + 1} splits "
            f"the 'Components at a glance' table — tables must be continuous."
        )