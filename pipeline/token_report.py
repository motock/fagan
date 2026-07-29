"""
Token usage summarization utility.

This module provides a single public function ``summarize_token_costs`` that reads a JSONL file containing per‑record token and cost metrics, applies optional filtering by timestamp, and aggregates totals per backend and per role.

The implementation follows the expectations defined in the acceptance tests:

* Missing or empty files return a zeroed summary.
* Malformed JSON lines are skipped silently.
* Records with missing fields default to sensible values (``unknown`` for ``backend``/``role``, ``0`` for token counts, ``0.0`` for cost).
* The optional ``since`` filter includes records whose timestamp is *greater than or equal to* the supplied boundary; unparseable timestamps are excluded.
* Aggregation results are returned as a dictionary with keys ``total_records``, ``by_backend`` and ``by_role``.
"""

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _parse_ts(ts: str | None) -> datetime | None:
    """Parse an ISO‑8601 timestamp string.

    Returns ``None`` if the input is missing or cannot be parsed.  The tests use
    timestamps with a trailing ``+00:00`` UTC offset; :func:`datetime.fromisoformat`
    handles this correctly.
    """
    if not ts:
        return None
    try:
        # fromisoformat accepts the format used in the test data.
        return datetime.fromisoformat(ts)
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------

def summarize_token_costs(
    path: str | Path,
    *,
    since: datetime | None = None,
) -> dict[str, Any]:
    """Summarize token usage from a JSONL file.

    Parameters
    ----------
    path:
        File system path to the ``.jsonl`` file containing one JSON object per line.
    since:
        Optional timestamp boundary; only records with timestamps *greater than or
        equal to* this value are included in the aggregation.  Records without a
        parsable timestamp are excluded.

    Returns
    -------
    dict
        ``{"total_records": int, "by_backend": dict, "by_role": dict}``
        where ``by_backend`` maps backend names to a dictionary containing
        ``input_tokens``, ``output_tokens`` and ``total_cost_usd``.  ``by_role``
        maps role names to a dictionary with the same token/cost fields plus a
        ``count`` key.
    """

    # Zero‑ed summary for missing or empty files.
    zero_summary = {"total_records": 0, "by_backend": {}, "by_role": {}}

    try:
        file_iter: Iterable[str] = Path(path).open("r", encoding="utf-8")
    except FileNotFoundError:
        return zero_summary

    total_records = 0
    by_backend: dict[str, dict[str, Any]] = {}
    by_role: dict[str, dict[str, Any]] = {}

    for raw_line in file_iter:
        line = raw_line.strip()
        if not line:
            continue
        try:
            record: Mapping[str, Any] = json.loads(line)
        except Exception:
            # Skip malformed JSON silently.
            continue

        # Apply the optional ``since`` filter.
        if since is not None:
            ts_str = record.get("ts")
            parsed_ts = _parse_ts(ts_str)
            if parsed_ts is None or parsed_ts < since:
                continue

        # Default values for missing fields.
        backend = record.get("backend") or "unknown"
        role = record.get("role") or "unknown"

        input_tokens_raw = record.get("input_tokens", 0)
        output_tokens_raw = record.get("output_tokens", 0)
        # Treat None as zero.
        try:
            input_tokens = int(input_tokens_raw) if input_tokens_raw is not None else 0
        except Exception:
            input_tokens = 0
        try:
            output_tokens = int(output_tokens_raw) if output_tokens_raw is not None else 0
        except Exception:
            output_tokens = 0

        cost_raw = record.get("total_cost_usd")
        try:
            cost = float(cost_raw) if cost_raw is not None else 0.0
        except Exception:
            cost = 0.0

        # Aggregate totals.
        total_records += 1

        # Backend aggregation
        backend_entry = by_backend.setdefault(
            backend,
            {"input_tokens": 0, "output_tokens": 0, "total_cost_usd": 0.0},
        )
        backend_entry["input_tokens"] += input_tokens
        backend_entry["output_tokens"] += output_tokens
        backend_entry["total_cost_usd"] += cost

        # Role aggregation (count and token/cost totals)
        role_entry = by_role.setdefault(
            role,
            {"count": 0, "input_tokens": 0, "output_tokens": 0, "total_cost_usd": 0.0},
        )
        role_entry["count"] += 1
        role_entry["input_tokens"] += input_tokens
        role_entry["output_tokens"] += output_tokens
        role_entry["total_cost_usd"] += cost

    return {
        "total_records": total_records,
        "by_backend": by_backend,
        "by_role": by_role,
    }
