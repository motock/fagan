"""Autonomous SDLC Agent Pipeline package.

Deliberately does NOT re-export server's public surface here (e.g. via
`from .server import *`): server.py defines an MCP tool function named
`checkpoint`, which collides with the `pipeline.checkpoint` submodule -
a star-import would shadow the submodule attribute with that function,
breaking `from pipeline import checkpoint`. Backward compatibility for
`import pipeline_mcp_server as p` is handled by the separate top-level
app/pipeline_mcp_server.py shim, which imports directly from `pipeline.server`
and does not go through this package's namespace.
"""