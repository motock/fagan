"""`.agent_done` completion-marker writer for scripts/local_agent_oracle.py.
Split out purely to keep local_agent_oracle.py under the project's
line-count target. The wrapper `write_done_marker` stays in the original
module and passes its own `CWD` in, so tests that monkeypatch `CWD` (or
`write_done_marker`) on the original module keep working.
"""
import json
import os
from datetime import datetime, timezone

_DONE_REASONS = {0: "done", 1: "error", 2: "parked", 3: "infra_failure"}


def write_done_marker_impl(cwd, rc: int) -> None:
    try:
        existing_path = cwd / ".agent_done"
        existing = json.loads(existing_path.read_text(encoding="utf-8"))
        if isinstance(existing, dict) and existing.get("exit_code") == 0 and rc != 0:
            return  # keep the proof of completion; a deliberate skip is not a failure
    except (OSError, ValueError):  # no readable done marker: fall through and write
        pass
    try:
        marker = {
            "reason": _DONE_REASONS.get(rc, "error"),
            "exit_code": rc,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        tmp = cwd / ".agent_done.tmp"
        tmp.write_text(json.dumps(marker) + "\n", encoding="utf-8")
        os.replace(tmp, cwd / ".agent_done")
    except Exception as e:  # noqa: BLE001 - a marker failure must never mask the run's exit code
        print(f"[warn] .agent_done marker not written: {e}", flush=True)
