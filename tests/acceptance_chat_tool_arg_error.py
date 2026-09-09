"""Acceptance oracle: a wrong-argument tool call must return an actionable error.

Root cause this grades (2026-09-09): ``_execute_tool`` splats the model's args
straight into the tool's ``execute`` callable and returns ``str(exc)`` on any
exception, so a wrong argument name surfaced to the model as the bare
``<lambda>() missing 2 required positional arguments: 'plan_name' and
'plan_json'`` -- which names neither the tool nor the arguments it actually
accepts, leaving the model nothing to self-correct with.

The enrichment must live in ``_execute_tool`` (not in the ``execute``
lambdas): tests/unit/test_chat_plan_tools.py pins a raw ``TypeError``
escaping ``TOOLS[name]["execute"](...)`` when called directly.
"""

import pytest

from app.chat import TOOLS, _execute_tool


class _NoHttpClient:
    """Fails loudly if a malformed call ever reaches the network."""

    def get(self, *args, **kwargs):
        raise AssertionError("a bad-argument call must not reach HTTP")

    def post(self, *args, **kwargs):
        raise AssertionError("a bad-argument call must not reach HTTP")


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _OkClient:
    def __init__(self):
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs.get("json")))
        return _Resp({"ok": True})


def _bad_save_plan_call():
    # The exact shape the chat model emitted in the live 2026-09-09 failure.
    return _execute_tool(
        "save_plan",
        {"name": "anagram-api", "plan": {"epics": []}},
        _NoHttpClient(),
        "http://test",
    )


def test_wrong_arg_names_return_an_error_not_a_result():
    result = _bad_save_plan_call()
    assert "error" in result
    assert "result" not in result


def test_wrong_arg_names_error_names_every_expected_param():
    message = _bad_save_plan_call()["error"]
    for expected in TOOLS["save_plan"]["params"]:
        assert expected in message, f"error must name expected param {expected!r}: {message!r}"


def test_wrong_arg_names_error_names_the_tool():
    assert "save_plan" in _bad_save_plan_call()["error"]


def test_wrong_arg_names_error_names_the_rejected_args():
    message = _bad_save_plan_call()["error"]
    assert "name" in message and "plan" in message


def test_missing_required_arg_error_names_the_expected_param():
    result = _execute_tool("get_plan", {}, _NoHttpClient(), "http://test")
    assert "error" in result
    assert "plan_name" in result["error"]
    assert "get_plan" in result["error"]


def test_correctly_named_args_still_execute():
    client = _OkClient()
    result = _execute_tool(
        "save_plan",
        {"plan_name": "demo", "plan_json": '{"epics":[]}'},
        client,
        "http://test",
    )
    assert result == {"result": {"ok": True}}
    assert client.posts == [("/api/plans/demo/save", {"plan_json": '{"epics":[]}'})]


def test_unknown_tool_error_is_unchanged():
    result = _execute_tool("no_such_tool", {}, _NoHttpClient(), "http://test")
    assert result == {"error": "unknown tool: no_such_tool"}


def test_non_signature_exceptions_keep_their_bare_message(monkeypatch):
    # tests/unit/test_chat_agent_loop.py::test_tool_exception_is_caught pins
    # {"error": "boom!"} exactly, and registers a fake entry with NO "params"
    # key -- so the enrichment must be scoped to signature mismatches and must
    # read params defensively.
    def _boom(http_client, api_base_url, **kwargs):
        raise RuntimeError("boom!")

    monkeypatch.setitem(TOOLS, "explode", {"execute": _boom})
    assert _execute_tool("explode", {}, None, "http://test") == {"error": "boom!"}


def test_entry_without_params_key_does_not_crash_the_dispatcher(monkeypatch):
    def _needs_an_arg(http_client, api_base_url, required, **kwargs):
        return {"got": required}

    monkeypatch.setitem(TOOLS, "needy", {"execute": _needs_an_arg})
    result = _execute_tool("needy", {}, None, "http://test")
    assert "error" in result
    assert "needy" in result["error"]


def test_registry_entries_keep_exactly_the_three_declared_keys():
    for name, entry in TOOLS.items():
        assert set(entry) == {"description", "params", "execute"}, name


@pytest.mark.parametrize("tool", ["save_plan", "ingest_plan", "dispatch_story"])
def test_every_params_key_is_accepted_by_its_own_execute(tool):
    # Guards the inverse drift: a declared param the execute callable would
    # reject is a broken contract the new error path would surface as a lie.
    import inspect

    sig = inspect.signature(TOOLS[tool]["execute"])
    accepts_kwargs = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    for param in TOOLS[tool]["params"]:
        assert accepts_kwargs or param in sig.parameters, f"{tool} cannot accept {param}"
