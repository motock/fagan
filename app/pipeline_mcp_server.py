"""Backward-compatibility shim.

The pipeline MCP server and its extracted modules now live in the ``pipeline``
package (``pipeline.server`` etc.). This thin module re-exports the server
module's entire public AND private surface so existing callers —
``from app import pipeline_mcp_server as p``, the MCP launch command,
``backend.py``'s lazy import, ``scripts/local_agent.py``,
``scripts/local_agent_oracle.py``, ``scripts/reset_false_positive_tests_passed.py``,
and ``test_backend.py`` — keep working unchanged.
"""

import sys
from pathlib import Path

# Launched directly (`python3 app/pipeline_mcp_server.py`, the MCP command),
# Python puts this file's own directory (app/) on sys.path[0], not the repo
# root - without this, `import pipeline.server` below can't find the
# sibling `pipeline` package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Re-export private names too (from pipeline.server import * skips _-prefixed).
# backend.py accesses _p._read_usage_state, tests access p._run_planner etc.
import pipeline.server as _server
from pipeline.server import *

for _name in dir(_server):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_server, _name)

mcp = _server.mcp

if __name__ == "__main__":
    mcp.run()