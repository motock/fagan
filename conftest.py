import os
import pytest

@pytest.fixture(autouse=True)
def _isolate_environ():
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
