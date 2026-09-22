"""Throwaway script: slice the merge-hold re-adjudication block out of
pipeline/advance.py into pipeline/advance_merge.py. Deleted before commit."""

from pathlib import Path

SRC_PATH = Path("pipeline/advance.py")
NEW_PATH = Path("pipeline/advance_merge.py")

START = '_MERGE_HOLD_REASON = "high risk held for human review"\n'
END = "def _adjudicate_merges(plan_name: str, summary: dict[str, Any]) -> None:\n"

SURVIVORS = [
    "class _ServerRef",
    "def _degraded_ci_branch",
    "def _advance_pipeline_locked",
    "def _story_dispatch_is_on_device",
    "def _count_on_device_in_progress_agents",
    "def _advance_pipeline_locked_impl",
    "def _adjudicate_merges",
]

HEADER = '''"""Advance-owned merge-hold re-adjudication.

The names this module reads that are bound at module level in
``pipeline.advance`` (``PIPELINE_AUTONOMY``, ``_merge_decision``,
``_merge_mod``, ``merge_adjudication_plan``) are resolved through
``pipeline.advance`` at call time via ``_ModuleRef``, so
``monkeypatch.setattr(pipeline.advance, NAME, ...)`` keeps landing.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .module_ref import _ModuleRef

PIPELINE_AUTONOMY = _ModuleRef("pipeline.advance", "PIPELINE_AUTONOMY")
_merge_decision = _ModuleRef("pipeline.advance", "_merge_decision")
_merge_mod = _ModuleRef("pipeline.advance", "_merge_mod")
merge_adjudication_plan = _ModuleRef("pipeline.advance", "merge_adjudication_plan")


'''

# (a) read
src = SRC_PATH.read_text()

# (b) anchors occur exactly once, START precedes END
assert src.count(START) == 1, f"START anchor count = {src.count(START)}"
assert src.count(END) == 1, f"END anchor count = {src.count(END)}"
i = src.index(START)
j = src.index(END)
assert i < j, "START does not precede END"

# (c) slice
block = src[i:j]

# (d) block contains the movers and none of the survivors
assert "_MERGE_HOLD_REASON" in block
assert "_readjudicate_parked_merge_hold" in block
for survivor in SURVIVORS:
    assert survivor not in block, f"survivor leaked into block: {survivor}"

# (e) write the new module
header = HEADER
if "from __future__ import annotations" in src.split("\n\n", 1)[0]:
    header = header.replace(
        '"""\n\nimport logging',
        '"""\n\nfrom __future__ import annotations\n\nimport logging',
        1,
    )
NEW_PATH.write_text(header + block)

# (f) write the original file with the re-export import inserted
CONCURRENCY = "from .concurrency import"
assert src.count(CONCURRENCY) == 1, f"concurrency import count = {src.count(CONCURRENCY)}"
REEXPORT = (
    "from .advance_merge import _MERGE_HOLD_REASON, _readjudicate_parked_merge_hold\n"
)
trimmed = src[:i] + src[j:]
k = trimmed.index(CONCURRENCY)
trimmed = trimmed[:k] + REEXPORT + trimmed[k:]
SRC_PATH.write_text(trimmed)

# (g) re-read and assert the block moved
assert block in NEW_PATH.read_text()
assert block not in SRC_PATH.read_text()

# (h) line counts
for path in (SRC_PATH, NEW_PATH):
    print(path, sum(1 for _ in path.open()))
