from tests.unit._app_js import _run_comms_js

# This test ensures that the module-level variable `commsHistory` is defined.
# The current implementation deletes the declaration, so this test will
# fail with a ReferenceError until the missing line is restored.

def test_comms_history_defined():
    # Load the module (the loader will execute the module code).
    _run_comms_js("globalThis.__wiring = { reset: [], export: [] };")
    # Try to read the variable. If it is missing, the eval will throw a
    # ReferenceError and the returned dict will contain a `__node_failed`
    # entry.
    result = _run_comms_js("globalThis.commsHistory")
    assert "__node_failed" not in result, "commsHistory should be defined"
