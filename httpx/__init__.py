# Minimal httpx stub for tests
class Timeout(Exception):
    pass
class TransportError(Exception):
    pass

def request(method, url, headers=None, timeout=None, **kwargs):
    raise NotImplementedError("httpx stub: network not available in tests")
