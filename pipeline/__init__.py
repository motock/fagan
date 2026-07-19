"""Autonomous SDLC Agent Pipeline package.

Re-exports the server module's public surface for backward compatibility with
external callers that do `import pipeline_mcp_server as p`. New code should
import from `pipeline.server` directly.
"""

from .server import *  # noqa: F401,F403
from . import server  # noqa: F401