import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "REFERENCE.md"
README = ROOT / "README.md"

AUTONOMY_ROW = (
    "| `PIPELINE_AUTONOMY` | `gated` | `dry-run` \\| `gated` \\| `full` |"
)
AUTO_MERGE_CELLS = [
    "`PIPELINE_AUTO_MERGE`",
    "`1`",
    (
        "Set to `0` to park every reviewed PR for a human merge; "
        "dispatch and review continue"
    ),
]
AUTO_MERGE_BULLET = (
    "- `PIPELINE_AUTO_MERGE=0` overrides every level above at the merge gate only "
    "— nothing else is gated."
)
README_SENTENCE = (
    "Set `PIPELINE_AUTO_MERGE=0` to keep everything automatic except merging; "
    "merges then happen only via `approve_merge` or by hand."
)


def test_reference_has_auto_merge_documentation() -> None:
    lines = REFERENCE.read_text(encoding="utf-8").splitlines()
    rows = [
        line
        for line in lines
        if line.startswith("| `PIPELINE_AUTO_MERGE` |")
    ]

    assert len(rows) == 1
    cells = [cell.strip() for cell in rows[0].strip().strip("|").split("|")]
    assert cells == AUTO_MERGE_CELLS

    row_index = lines.index(rows[0])
    assert lines[row_index - 1] == AUTONOMY_ROW
    assert AUTO_MERGE_BULLET in lines


def test_readme_has_auto_merge_sentence() -> None:
    text = README.read_text(encoding="utf-8")
    assert README_SENTENCE in text


def test_readme_has_exactly_eleven_h2_headings() -> None:
    text = README.read_text(encoding="utf-8")
    headings = re.findall(r"^##(?!#)\s+.+$", text, flags=re.MULTILINE)
    assert len(headings) == 11
