class MCPServer:
    """Minimal stub for MCPServer used in tests."""
    def __init__(self, *args, **kwargs):
        pass
    def tool(self, *args, **kwargs):
        def decorator(func):
            return func
        return decorator
