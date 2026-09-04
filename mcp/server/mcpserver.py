"""Stub MCPServer for testing.

This module provides a minimal MCPServer class used by the pipeline server
to satisfy imports during test collection. The real implementation is not
required for the unit tests in this repository.
"""

class MCPServer:
    def __init__(self, *args, **kwargs):
        pass

    def tool(self, *args, **kwargs):
        def decorator(func):
            return func
        return decorator

    def stop(self):
        pass
