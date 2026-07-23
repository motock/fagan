import os
import pytest

# Cleared here, at conftest.py's own module-import time, because pytest
# imports conftest.py before collecting test modules - clearing only inside
# an autouse fixture is too late for module-level constants (e.g.
# pipeline.usage's SESSION_PAUSE_THRESHOLD, scripts/local_agent.py's
# PARK_ENABLED) that read PIPELINE_*/LOCAL_AGENT_* via os.environ.get(...)
# once at their own import time, during collection. LOCAL_AGENT_* matters
# when the suite runs inside a dispatched agent's own bash tool: the
# scheduler plist sets LOCAL_AGENT_PARK_ENABLED=0, which leaks into the
# agent's pytest subprocess and makes every park-expecting guard test fail
# on a correct codebase (observed live 2026-07-22 - the agent could never
# see green, so it "fixed" a correct file until it broke).
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
