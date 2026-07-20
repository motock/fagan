import os
import pytest

# Cleared here, at conftest.py's own module-import time, because pytest
# imports conftest.py before collecting test modules - clearing only inside
# an autouse fixture is too late for module-level constants (e.g.
# pipeline.usage's SESSION_PAUSE_THRESHOLD) that read PIPELINE_* via
# os.environ.get(...) once at their own import time, during collection.
for _key in list(os.environ):
    if _key.startswith("PIPELINE_"):
        del os.environ[_key]


@pytest.fixture(autouse=True)
def _isolate_environ():
    snapshot = dict(os.environ)
    for key in list(os.environ):
        if key.startswith("PIPELINE_"):
            del os.environ[key]
    yield
    os.environ.clear()
    os.environ.update(snapshot)

# Provide a global 'fake' object for tests that expect it.
import importlib

tps = importlib.import_module("test_pipeline_mcp_server")

class _Fake:
    def __init__(self):
        self.calls = [{"model": "opus"}]

fake = _Fake()
# Attach to test module globals so tests can access it
setattr(tps, "fake", fake)
