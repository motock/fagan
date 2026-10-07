"""Claude spend surfaced in the scorecard.

harness.py records each cell's Claude spend (dispatch + review, from
cell_cost.claude_spend_usd) in its result dict as "claude_usd";
scorecard.aggregate() sums that per (task, model) group and render() adds a
"Claude $ / success" column to the Per-model totals table: total spend
divided by successful cells, so a model's cost is compared against its yield
rather than its trial count. A model with no successes prints "-" (never a
ZeroDivisionError), and cells from older runs - which carry no "claude_usd"
key, or a null one - count as 0.0.

The Per-model totals table is a shared artifact that a later story extends
with a column of its own, so these tests look the column up by its header
name and never pin the full header line, the column count, or the footnote's
wording.
"""

import inspect

from tests.benchmark import harness, scorecard


def _cell(task: str, model: str, *, ok: bool = True, **extra) -> dict:
    """A minimal harness result cell; ``ok=False`` makes it a failed cell."""
    cell = {
        "task": task,
        "model": model,
        "final_status": "done" if ok else "error",
        "groundtruth_passed": bool(ok),
    }
    cell.update(extra)
    return cell


def _per_model_section(cells: list[dict]) -> tuple[int, list[str]]:
    """(index of the section heading, its contiguous markdown table lines)."""
    lines = scorecard.render(cells).splitlines()
    start = next(i for i, ln in enumerate(lines) if "Per-model totals" in ln)
    table: list[str] = []
    for ln in lines[start + 1 :]:
        if ln.startswith("|"):
            table.append(ln)
        elif table:
            break
    return start, table


def _per_model_table(cells: list[dict]) -> tuple[list[str], list[str], dict]:
    """Parse the Per-model totals table.

    Returns (header cells, separator cells, {model: row cells}); every cell is
    stripped, so a test can compare a bare token like "2.00" no matter how the
    row is spaced.
    """
    _, table = _per_model_section(cells)
    assert len(table) >= 2, "Per-model totals table needs a header and separator row"
    parsed = [[c.strip() for c in row.strip().strip("|").split("|")] for row in table]
    return parsed[0], parsed[1], {row[0]: row for row in parsed[2:]}


def _cost_column(header: list[str]) -> int:
    assert "Claude $ / success" in header, (
        "Per-model totals header must carry a 'Claude $ / success' column"
    )
    return header.index("Claude $ / success")


def _cost_cells(cells: list[dict]) -> dict[str, str]:
    """{model: Claude $ / success cell} from the Per-model totals table."""
    header, sep, rows = _per_model_table(cells)
    col = _cost_column(header)
    assert len(sep) > col and sep[col] and set(sep[col]) <= {"-", ":"}, (
        "separator row needs a dash cell under the Claude $ / success header"
    )
    out = {}
    for model, row in rows.items():
        assert len(row) > col, f"row for {model!r} lacks the Claude $ / success cell"
        out[model] = row[col]
    return out


def _footnote(cells: list[dict]) -> str:
    """render()'s prose after the Per-model totals table (the footnote block)."""
    lines = scorecard.render(cells).splitlines()
    start, table = _per_model_section(cells)
    return "\n".join(lines[start + 1 + len(table) :])


# --- harness wiring ---------------------------------------------------------


def test_harness_main_imports_cell_cost_directly_above_models():
    """main() must import cell_cost locally (harness.py runs as a script with
    tests/benchmark on sys.path, exactly like its MODELS import), and the
    plain import must come before the `from models import MODELS` import or
    ruff's isort rule (I001) fails the lint gate."""
    lines = [ln.strip() for ln in inspect.getsource(harness.main).splitlines()]
    assert "import cell_cost" in lines, "main() must import cell_cost"
    i = lines.index("import cell_cost")
    assert lines[i + 1] == "from models import MODELS", (
        "'import cell_cost' must sit directly above 'from models import MODELS'"
    )


def test_harness_main_records_claude_usd_in_result():
    """main() must record the cell's total Claude spend under "claude_usd"."""
    src = inspect.getsource(harness.main)
    assert "cell_cost.claude_spend_usd(cell)" in src
    assert '"claude_usd"' in src, "the result dict must gain a 'claude_usd' key"
    assert "total_usd" in src, "the recorded value must be the cell's total spend"
    gt = src.index('"groundtruth_tail"')
    assert '"claude_usd"' in src[gt:], "'claude_usd' must be a result-dict key"


# --- aggregate() ------------------------------------------------------------


def test_aggregate_exposes_claude_usd_per_task_model_group():
    stats = scorecard.aggregate(
        [
            _cell("t1", "m", claude_usd=1.5),
            _cell("t1", "m", claude_usd=2.25),
            _cell("t2", "m"),  # older run: no key at all
        ]
    )
    assert stats[("t1", "m")]["claude_usd"] == 3.75
    assert stats[("t2", "m")]["claude_usd"] == 0.0


def test_aggregate_treats_missing_or_none_claude_usd_as_zero():
    stats = scorecard.aggregate(
        [
            _cell("t1", "m"),
            _cell("t1", "m", claude_usd=None),
            _cell("t1", "m", claude_usd=0.25),
        ]
    )
    assert stats[("t1", "m")]["claude_usd"] == 0.25


def test_aggregate_rounds_claude_usd_to_four_decimals():
    stats = scorecard.aggregate(
        [
            _cell("t1", "m", claude_usd=0.11111),
            _cell("t1", "m", claude_usd=0.22222),
        ]
    )
    assert stats[("t1", "m")]["claude_usd"] == 0.3333


# --- render(): the Claude $ / success column ---------------------------------


def test_per_model_header_has_claude_cost_column():
    header, sep, _ = _per_model_table([_cell("t1", "m")])
    col = _cost_column(header)
    assert sep[col] and set(sep[col]) <= {"-", ":"}


def test_render_reports_claude_cost_per_success():
    costs = _cost_cells(
        [
            _cell("t1", "m", claude_usd=1.0),
            _cell("t2", "m", claude_usd=3.0),
        ]
    )
    assert costs["m"] == "2.00"


def test_render_zero_success_model_shows_dash_not_zero_division():
    costs = _cost_cells(
        [
            _cell("t1", "z", ok=False, claude_usd=7.5),
            _cell("t2", "z", ok=False, claude_usd=2.5),
        ]
    )
    assert costs["z"] == "-"


def test_render_divides_by_successes_not_trials():
    """A failed cell still spends, so its cost belongs in the numerator while
    only successful cells belong in the denominator."""
    costs = _cost_cells(
        [
            _cell("t1", "m", ok=False, claude_usd=7.5),
            _cell("t2", "m", claude_usd=2.5),
        ]
    )
    assert costs["m"] == "10.00"


def test_render_zero_spend_with_successes_shows_zero():
    """ "0.00" (successes but no spend) must stay distinct from "-" (no
    successes)."""
    costs = _cost_cells([_cell("t1", "m", claude_usd=0.0)])
    assert costs["m"] == "0.00"


def test_render_treats_missing_or_none_spend_as_zero():
    costs = _cost_cells(
        [
            _cell("t1", "m"),  # older run: no key
            _cell("t2", "m", claude_usd=None),
            _cell("t3", "m", claude_usd=2.0),
            _cell("t1", "old"),  # a model with no spend data at all
        ]
    )
    assert costs["m"] == "0.67"  # 2.0 spread over 3 successes
    assert costs["old"] == "0.00"


def test_footnote_explains_the_claude_cost_column():
    """The footnote block must mention the new column; its exact wording is
    deliberately not pinned."""
    assert "Claude $ / success" in _footnote([_cell("t1", "m", claude_usd=1.0)])
