import os

import pytest

# Clear PIPELINE_*/LOCAL_AGENT_* env vars at conftest import time, before any
# test module is collected. Several pipeline modules read these via
# os.environ.get(...) ONCE at their own import time (e.g. pipeline.ci's
# PIPELINE_MERGE_CI_GATE, pipeline.config's SESSION_PAUSE_THRESHOLD). When the
# suite runs inside a dispatched agent's own bash tool, the scheduler plist
# sets these (e.g. PIPELINE_MERGE_CI_GATE=0, PIPELINE_PAUSE_THRESHOLD=101),
# which leaks into the pytest subprocess. If a test module outside tests/unit
# (e.g. tests/benchmark) imports such a pipeline module while the var is set,
# the module-level constant binds to the leaked value and pollutes the shared
# module for the rest of the session, making unrelated tests fail on a correct
# codebase. Clearing here, at the suite root, covers every test directory.
_ISOLATED_ENV_PREFIXES = ("PIPELINE_", "LOCAL_AGENT_")

for _key in list(os.environ):
    if _key.startswith(_ISOLATED_ENV_PREFIXES):
        del os.environ[_key]


@pytest.fixture(autouse=True)
def _isolate_environ():
    snapshot = dict(os.environ)
    for key in list(os.environ):
        if key.startswith(_ISOLATED_ENV_PREFIXES):
            del os.environ[key]
    yield
    os.environ.clear()
    os.environ.update(snapshot)
