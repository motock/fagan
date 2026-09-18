# Minimal stub for httpx to satisfy imports in tests

class Response:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json

async def get(*args, **kwargs):
    return Response()

async def post(*args, **kwargs):
    return Response()

# expose a simple client class
class Client:
    def __init__(self, *args, **kwargs):
        pass
    async def get(self, *args, **kwargs):
        return Response()
    async def post(self, *args, **kwargs):
        return Response()

# expose a simple HTTPError exception
class HTTPError(Exception):
    pass
