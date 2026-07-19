import os
import pytest

@pytest.fixture(autouse=True)
def _isolate_environ():
    snapshot = dict(os.environ)
    for key in list(os.environ):
        if key.startswith("PIPELINE_"):
            del os.environ[key]
    yield
    os.environ.clear()
    os.environ.update(snapshot)
