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

# REPO_ROOT is scrubbed by exact name (it carries no PIPELINE_ prefix). The
# scheduler plist exports REPO_ROOT=/nonexistent-repo-root-set-per-plan-only
# (the "set per plan only" sentinel) into every dispatched agent's bash tool,
# and pipeline/server.py binds REPO_ROOT from the env ONCE at import time.
# Plans whose manifest records no repo_root then resolve to that sentinel via
# _repo_root_for, so any test that dispatches without stubbing repo_root fails
# with "refusing to set up a worktree for a missing repository". The suite must
# be hermetic against an ambient REPO_ROOT: tests that need one set it
# themselves (monkeypatch.setenv) or record it in the plan manifest.
if "REPO_ROOT" in os.environ:
    del os.environ["REPO_ROOT"]

# CFG env bootstrap: pipeline/__init__.py reads <repo_root>/.pipeline.env at import time; this opt-out makes that loader inert for the whole pytest run so a developer's repo-root file (e.g. PLAN_DIR written by CFG-D2's standalone-setup.sh) never leaks into the suite. Set AFTER the scrub loop above (it deletes every PIPELINE_* key) and before any pipeline import can occur.
os.environ["PIPELINE_SKIP_ENV_FILE"] = "1"

@pytest.fixture(autouse=True)
def _isolate_environ():
    snapshot = dict(os.environ)
    for key in list(os.environ):
        if key.startswith(_ISOLATED_ENV_PREFIXES):
            del os.environ[key]
    yield
    os.environ.clear()
    os.environ.update(snapshot)
