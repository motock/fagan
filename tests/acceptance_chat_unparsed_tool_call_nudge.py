"""Acceptance oracle: a ``[TOOL_CALL]`` marker that fails to parse must cost
one re-prompt (nudge) inside the existing turn budget, not end the turn.

Root cause this grades: ``ChatService.execute_turn`` treats
"``_parse_tool_calls(response)`` returned nothing" as "the model is done" and
returns the raw text as the final reply.  When the model *did* emit a
``[TOOL_CALL]`` marker but the payload inside it failed to parse (trailing
comma, missing closer, missing key, prose instead of JSON), that text is a
stalled tool call, not a conversational reply -- the turn must be spent on one
nudge re-prompt and the loop must then continue exactly as it does today for a
normal tool-result turn.

Contract under test (every driver here is a fake; no live LLM calls):

1. ``_looks_like_unparsed_tool_call(response) -> bool``: True iff
   ``[TOOL_CALL]`` appears in *response* AND ``_parse_tool_calls(response)``
   returns ``[]``; False when the marker is absent entirely (the existing
   conversational fast path) or when the text parses to >= 1 call.
2. ``execute_turn`` branches on that detector in its ``not parsed`` branch:
   True -> the next driver call receives a nudge prompt naming the malformed
   text verbatim plus the exact expected tag/JSON shape, and the loop
   continues; False -> return immediately, exactly as today.
3. The nudge spends a real turn from the existing ``self._max_turns`` budget
   (no separate counter, no state beyond the loop's own variables): a driver
   that never parses exhausts the budget and returns the same
   ``(turn cap reached)`` suffix the loop already produces today.

Precedent followed: the review loop's ``nudged``/``findings_nudged`` logic in
``app/backend_ollama.py`` (re-nudge on each remaining turn; never a new
turn-budget counter).

Run: pytest -q tests/acceptance_chat_unparsed_tool_call_nudge.py
"""

from __future__ import annotations

import inspect
import re

from app import chat
from app.chat import _looks_like_unparsed_tool_call

# ---------------------------------------------------------------------------
# Scripted model outputs
# ---------------------------------------------------------------------------

# The malformed text from the story: the [TOOL_CALL] marker is present but the
# JSON inside has a trailing comma, so _parse_tool_calls returns [] for it.
MALFORMED_RESPONSE = (
    '[TOOL_CALL]{"name": "decompose", "args": {"goal": "x"},}[/TOOL_CALL]'
)

# The well-formed call from the story's success criterion 1, verbatim.
WELL_FORMED_DECOMPOSE = (
    '[TOOL_CALL]{"name": "decompose", "args": {"goal": "x"}}[/TOOL_CALL]'
)

# The nudge prompt must restate the required shape with this exact substring.
EXPECTED_SHAPE = '[TOOL_CALL]{"name": "<tool>", "args": {...}}[/TOOL_CALL]'

PLAIN_REPLY = "Sure, here is some info about plans."
PLAIN_REPLY_2 = "All done - no tools were needed."

# Every variant contains the literal [TOOL_CALL] marker yet parses to zero
# calls (bad JSON / missing closer / missing or mistyped keys / prose).
MALFORMED_VARIANTS = [
    MALFORMED_RESPONSE,                                         # trailing comma
    '[TOOL_CALL]{"name": "decompose", "args": {"goal": "x"}}',  # missing closer
    '[TOOL_CALL]{"name": "decompose"}[/TOOL_CALL]',             # missing args key
    '[TOOL_CALL]{"args": {"goal": "x"}}[/TOOL_CALL]',           # missing name key
    '[TOOL_CALL]call decompose with goal x[/TOOL_CALL]',        # prose, not JSON
    '[TOOL_CALL]{"name": 1, "args": {}}[/TOOL_CALL]',           # name not a str
]

# Marker present but at least one call parses -> NOT an unparsed stall; the
# normal tool path must run instead of a nudge.
PARSEABLE_MIX = (
    '[TOOL_CALL]oops not json[/TOOL_CALL] '
    '[TOOL_CALL]{"name": "decompose", "args": {}}[/TOOL_CALL]'
)

# A tool-result echo block carries no [TOOL_CALL] opener at all.
TOOL_RESULT_ECHO = '[TOOL_RESULT name=decompose]{"ok": 1}[/TOOL_RESULT]'


# ---------------------------------------------------------------------------
# Fakes (no live LLM, no live HTTP)
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _RecordingClient:
    """Records POSTs and answers them with a fixed decompose-shaped payload."""

    def __init__(self):
        self.posts: list[tuple[str, object]] = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs.get("json")))
        return _Resp({"epics": [], "name": "fake-decompose"})

    def get(self, *args, **kwargs):
        raise AssertionError("no GET is expected in this story's tests")


class _FakeDriver:
    """Scripted stand-in for the chat backend driver.

    Returns the scripted responses in order; once the script is exhausted it
    returns *default* forever.  Every ``complete()`` call is recorded (prompt
    and system) so tests can assert exactly what the loop fed back to the
    model.  *hard_cap* converts a runaway loop into a fast, loud failure
    instead of a hung pytest session.
    """

    def __init__(self, responses, *, default="All done.", hard_cap=64):
        self._responses = list(responses)
        self._default = default
        self._hard_cap = hard_cap
        self.prompts: list[str] = []
        self.systems: list[str] = []

    def complete(self, prompt, *, system=None, model="", **_kw):
        self.prompts.append(prompt)
        self.systems.append(system)
        if len(self.prompts) > self._hard_cap:
            raise AssertionError(
                f"driver.complete called {len(self.prompts)} times "
                f"(hard cap {self._hard_cap}): the chat loop kept "
                "re-prompting past max_turns instead of stopping"
            )
        if self._responses:
            return self._responses.pop(0)
        return self._default


def _make_service(driver, *, max_turns):
    client = _RecordingClient()
    svc = chat.ChatService(
        driver=driver,
        http_client=client,
        api_base_url="http://test",
        max_turns=max_turns,
    )
    return svc, client


# ---------------------------------------------------------------------------
# 1. The detector itself
# ---------------------------------------------------------------------------
def test_detector_is_a_module_level_single_argument_predicate():
    assert callable(chat._looks_like_unparsed_tool_call)
    sig = inspect.signature(_looks_like_unparsed_tool_call)
    assert len(sig.parameters) == 1


def test_detector_flags_marker_text_that_parses_to_zero_calls():
    for text in MALFORMED_VARIANTS:
        assert _looks_like_unparsed_tool_call(text) is True, text


def test_detector_rejects_text_without_any_tool_call_marker():
    # The existing, correct conversational fast path must stay False.
    assert _looks_like_unparsed_tool_call(PLAIN_REPLY) is False
    assert _looks_like_unparsed_tool_call("") is False
    assert _looks_like_unparsed_tool_call(TOOL_RESULT_ECHO) is False


def test_detector_accepts_marker_text_that_parses_to_a_real_call():
    # Marker present AND parseable -> not a stall; the happy path must run.
    assert _looks_like_unparsed_tool_call(WELL_FORMED_DECOMPOSE) is False
    assert _looks_like_unparsed_tool_call(PARSEABLE_MIX) is False


# ---------------------------------------------------------------------------
# 2. execute_turn: malformed turn -> one nudge -> recovery (criterion 1)
# ---------------------------------------------------------------------------
def test_malformed_tool_call_text_is_nudged_then_recovered():
    driver = _FakeDriver([MALFORMED_RESPONSE, WELL_FORMED_DECOMPOSE, PLAIN_REPLY])
    svc, client = _make_service(driver, max_turns=2)

    result = svc.execute_turn("decompose the goal x")

    assert result["turns"] == 2
    assert len(result["tool_calls"]) == 1
    entry = result["tool_calls"][0]
    assert entry["name"] == "decompose"
    assert entry["args"] == {"goal": "x"}
    assert "error" not in entry["result"]
    assert "result" in entry["result"]
    assert client.posts == [("/api/decompose", {"request": "x"})]


def test_nudge_prompt_names_the_malformed_text_and_the_expected_shape():
    driver = _FakeDriver([MALFORMED_RESPONSE, WELL_FORMED_DECOMPOSE, PLAIN_REPLY])
    svc, _client = _make_service(driver, max_turns=2)

    svc.execute_turn("decompose the goal x")

    assert len(driver.prompts) >= 2, (
        "a malformed [TOOL_CALL] turn must re-prompt the model instead of "
        "returning; the driver only saw the original message"
    )
    nudge = driver.prompts[1]
    # (a) the exact text the model produced that failed to parse, verbatim
    assert MALFORMED_RESPONSE in nudge
    # (b) a restatement of the required tag/JSON shape, verbatim
    assert EXPECTED_SHAPE in nudge
    # The nudge travels through the prompt channel; the system prompt is
    # untouched, exactly as on a normal tool-result turn.
    assert driver.systems[1] == driver.systems[0]


# ---------------------------------------------------------------------------
# 3. execute_turn: the nudge spends a real budgeted turn (criterion 2)
# ---------------------------------------------------------------------------
def test_always_malformed_driver_exhausts_max_turns_gracefully():
    driver = _FakeDriver([], default=MALFORMED_RESPONSE)
    svc, client = _make_service(driver, max_turns=3)

    result = svc.execute_turn("decompose the goal x")  # must not raise

    assert result["turns"] == 3
    assert result["reply"].endswith("(turn cap reached)")
    assert MALFORMED_RESPONSE in result["reply"]
    assert result["tool_calls"] == []
    assert client.posts == []
    # One driver call per budgeted turn -- the loop never ran past max_turns.
    assert len(driver.prompts) == 3


def test_malformed_first_turn_with_min_budget_hits_cap_gracefully():
    driver = _FakeDriver([], default=MALFORMED_RESPONSE)
    svc, _client = _make_service(driver, max_turns=1)

    result = svc.execute_turn("decompose the goal x")  # must not raise

    assert result["turns"] == 1
    assert result["reply"].endswith("(turn cap reached)")
    assert result["tool_calls"] == []
    assert len(driver.prompts) == 1


# ---------------------------------------------------------------------------
# 4. Regression guard: a plain reply still returns immediately (criterion 3)
# ---------------------------------------------------------------------------
def test_plain_conversational_reply_still_returns_immediately():
    driver = _FakeDriver([PLAIN_REPLY])
    svc, client = _make_service(driver, max_turns=5)

    result = svc.execute_turn("what can you tell me about plans?")

    assert result["turns"] == 1
    assert result["reply"] == PLAIN_REPLY
    assert result["tool_calls"] == []
    assert client.posts == []
    assert len(driver.prompts) == 1  # no second driver call: fast path intact


# ---------------------------------------------------------------------------
# 5. Unchanged happy path (criterion 4)
# ---------------------------------------------------------------------------
def test_well_formed_first_turn_happy_path_is_unchanged():
    driver = _FakeDriver([WELL_FORMED_DECOMPOSE, PLAIN_REPLY])
    svc, client = _make_service(driver, max_turns=5)

    result = svc.execute_turn("decompose the goal x")

    assert result["turns"] == 2
    assert result["reply"] == PLAIN_REPLY
    assert len(result["tool_calls"]) == 1
    entry = result["tool_calls"][0]
    assert entry["name"] == "decompose"
    assert entry["args"] == {"goal": "x"}
    assert "result" in entry["result"] and "error" not in entry["result"]
    assert client.posts == [("/api/decompose", {"request": "x"})]
    assert len(driver.prompts) == 2
    # The second prompt is the normal tool-result block, never a nudge.
    assert "[TOOL_RESULT name=decompose]" in driver.prompts[1]
    assert MALFORMED_RESPONSE not in driver.prompts[1]


# ---------------------------------------------------------------------------
# 6. After a nudge, the loop continues exactly as for a normal tool-result turn
# ---------------------------------------------------------------------------
def test_recovered_call_flows_into_a_normal_tool_result_turn():
    driver = _FakeDriver([MALFORMED_RESPONSE, WELL_FORMED_DECOMPOSE, PLAIN_REPLY])
    svc, client = _make_service(driver, max_turns=4)

    result = svc.execute_turn("decompose the goal x")

    # malformed turn + nudge turn + post-tool reply turn: the nudge spent a
    # real turn from the existing budget.
    assert result["turns"] == 3
    assert result["reply"] == PLAIN_REPLY  # final reply, no cap suffix
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["name"] == "decompose"
    assert client.posts == [("/api/decompose", {"request": "x"})]
    assert len(driver.prompts) == 3
    assert MALFORMED_RESPONSE in driver.prompts[1]
    assert EXPECTED_SHAPE in driver.prompts[1]
    assert "[TOOL_RESULT name=decompose]" in driver.prompts[2]


# ---------------------------------------------------------------------------
# 7. No nudge state may leak past the loop's own variables
# ---------------------------------------------------------------------------
def test_nudge_does_not_persist_state_across_execute_turn_calls():
    driver = _FakeDriver([MALFORMED_RESPONSE, PLAIN_REPLY, PLAIN_REPLY_2])
    svc, _client = _make_service(driver, max_turns=10)

    first = svc.execute_turn("decompose the goal x")
    assert first["tool_calls"] == []
    assert first["reply"] == PLAIN_REPLY

    before = len(driver.prompts)
    second = svc.execute_turn("now just answer plainly")

    assert second["turns"] == 1
    assert second["reply"] == PLAIN_REPLY_2
    assert second["tool_calls"] == []
    assert len(driver.prompts) - before == 1  # the second turn made exactly one call


# ---------------------------------------------------------------------------
# 8. Wiring: the branch lives in execute_turn and reuses the existing budget
# ---------------------------------------------------------------------------
def test_execute_turn_branches_on_the_detector():
    src = inspect.getsource(chat.ChatService.execute_turn)
    assert "_looks_like_unparsed_tool_call" in src


def test_loop_still_budgets_turns_with_self_max_turns():
    src = inspect.getsource(chat.ChatService.execute_turn)
    assert "self._max_turns" in src


def test_no_new_turn_budget_counter_attribute_is_added():
    src = inspect.getsource(chat.ChatService)
    assigned = set(re.findall(r"self\.(_\w*turn\w*)\s*=", src))
    assert assigned <= {"_max_turns"}