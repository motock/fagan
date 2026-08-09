"""Tests for ``pipeline.rebrief.detect_unsatisfiable_signal``.

This is a pure, standard-library-only diagnostic helper that inspects failure
evidence text collected from a struggling attempt and returns a short
human-readable reason string when the evidence indicates the story cannot be
satisfied as written (an API it needs does not exist or does not accept the
arguments the tests pass). It returns ``None`` otherwise.

The function must never raise - it runs on an already-failing path, so an
exception here must never worsen a retry.
"""
import pytest

from pipeline import rebrief

# ---------------------------------------------------------------------------
# Existence / signature contract
# ---------------------------------------------------------------------------

def test_function_exists_as_a_module_level_callable():
    assert hasattr(rebrief, "detect_unsatisfiable_signal"), (
        "pipeline.rebrief must define detect_unsatisfiable_signal"
    )
    assert callable(rebrief.detect_unsatisfiable_signal)


def test_function_is_a_module_level_def_not_a_nested_helper():
    import inspect

    # It should be a plain function defined at module level in pipeline.rebrief,
    # not a lambda or a nested closure.
    obj = rebrief.detect_unsatisfiable_signal
    assert inspect.isfunction(obj), "detect_unsatisfiable_signal must be a function"
    assert obj.__module__ == rebrief.__name__, (
        "detect_unsatisfiable_signal must be defined in pipeline.rebrief"
    )


def test_function_has_a_single_positional_parameter_named_evidence():
    import inspect

    sig = inspect.signature(rebrief.detect_unsatisfiable_signal)
    params = list(sig.parameters.values())
    assert len(params) == 1, (
        "detect_unsatisfiable_signal must take exactly one parameter"
    )
    assert params[0].name == "evidence", (
        "the single parameter must be named 'evidence'"
    )
    # It must be callable positionally (no required-only keyword arg).
    assert params[0].kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_ONLY,
    )


def test_return_annotation_is_str_or_none():
    import inspect
    import typing

    sig = inspect.signature(rebrief.detect_unsatisfiable_signal)
    annotation = sig.return_annotation
    # Accept either the string form or the resolved typing form.
    candidates = {annotation, str(annotation)}
    assert (
        "None" in candidates
        or annotation is None
        or typing.get_origin(annotation) is typing.Union
        or "str" in str(annotation)
    ), f"return annotation should mention str | None, got {annotation!r}"


# ---------------------------------------------------------------------------
# POSITIVE: one test per signal
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "signal,fragment",
    [
        ("unexpected keyword argument", "got an unexpected keyword argument 'foo'"),
        ("takes no keyword arguments", "function takes no keyword arguments"),
        ("cannot import name", "cannot import name 'X' from 'm'"),
        ("has no attribute", "'module' has no attribute 'thing'"),
        ("is not defined", "name 'foo' is not defined"),
    ],
)
def test_positive_signal_returns_non_none_reason_mentioning_signal(signal, fragment):
    reason = rebrief.detect_unsatisfiable_signal(fragment)
    assert reason is not None, f"expected a reason for signal: {signal!r}"
    assert isinstance(reason, str), "reason must be a string"
    assert len(reason) > 0, "reason must not be empty"
    assert len(reason) <= 200, f"reason must be under ~200 chars, got {len(reason)}: {reason!r}"
    # The reason must name the signal that matched so a human knows why it fired.
    assert signal in reason.lower(), (
        f"reason must mention the signal {signal!r}; got {reason!r}"
    )


def test_positive_unexpected_keyword_argument():
    reason = rebrief.detect_unsatisfiable_signal(
        "TypeError: collect() got an unexpected keyword argument 'limit'"
    )
    assert reason is not None
    assert "unexpected keyword argument" in reason.lower()


def test_positive_takes_no_keyword_arguments():
    reason = rebrief.detect_unsatisfiable_signal(
        "TypeError: f() takes no keyword arguments"
    )
    assert reason is not None
    assert "takes no keyword arguments" in reason.lower()


def test_positive_cannot_import_name():
    reason = rebrief.detect_unsatisfiable_signal(
        "ImportError: cannot import name 'Missing' from 'pkg'"
    )
    assert reason is not None
    assert "cannot import name" in reason.lower()


def test_positive_has_no_attribute():
    reason = rebrief.detect_unsatisfiable_signal(
        "AttributeError: module 'pkg' has no attribute 'thing'"
    )
    assert reason is not None
    assert "has no attribute" in reason.lower()


def test_positive_is_not_defined():
    reason = rebrief.detect_unsatisfiable_signal(
        "NameError: name 'foo' is not defined"
    )
    assert reason is not None
    assert "is not defined" in reason.lower()


# ---------------------------------------------------------------------------
# Case-insensitivity
# ---------------------------------------------------------------------------

def test_matching_is_case_insensitive():
    reason = rebrief.detect_unsatisfiable_signal(
        "TYPEERROR: GOT AN UNEXPECTED KEYWORD ARGUMENT 'X'"
    )
    assert reason is not None
    assert "unexpected keyword argument" in reason.lower()


# ---------------------------------------------------------------------------
# NEGATIVE / BOUNDARY: all must return None
# ---------------------------------------------------------------------------

def test_none_input_returns_none():
    assert rebrief.detect_unsatisfiable_signal(None) is None


def test_empty_string_returns_none():
    assert rebrief.detect_unsatisfiable_signal("") is None


def test_whitespace_only_string_returns_none():
    assert rebrief.detect_unsatisfiable_signal("   \n\t  \r\n ") is None


def test_non_string_input_returns_none_without_raising():
    # An int input must not raise; it returns None.
    assert rebrief.detect_unsatisfiable_signal(12345) is None


def test_non_string_list_input_returns_none_without_raising():
    assert rebrief.detect_unsatisfiable_signal(["got an unexpected keyword argument"]) is None


def test_non_string_dict_input_returns_none_without_raising():
    assert rebrief.detect_unsatisfiable_signal({"evidence": "is not defined"}) is None


def test_non_string_bytes_input_returns_none_without_raising():
    # bytes is not str; must not raise even though it could decode.
    assert rebrief.detect_unsatisfiable_signal(b"has no attribute") is None


def test_ordinary_failing_test_output_returns_none():
    evidence = (
        "AssertionError: assert 1 == 2\n"
        "  +  where 1 = foo()\n"
        "  +  and  2 = expected\n"
        "tests/unit/test_thing.py:42: AssertionError"
    )
    assert rebrief.detect_unsatisfiable_signal(evidence) is None


def test_traceback_without_signals_returns_none():
    evidence = (
        "Traceback (most recent call last):\n"
        "  File 'x.py', line 1, in <module>\n"
        "    raise ValueError('boom')\n"
        "ValueError: boom"
    )
    assert rebrief.detect_unsatisfiable_signal(evidence) is None


# ---------------------------------------------------------------------------
# PRECEDENCE: first matching signal wins in the documented order
# ---------------------------------------------------------------------------

def test_precedence_unexpected_keyword_argument_over_takes_no_keyword_arguments():
    # Both signals present; unexpected keyword argument is higher precedence.
    evidence = "got an unexpected keyword argument 'x' and takes no keyword arguments"
    reason = rebrief.detect_unsatisfiable_signal(evidence)
    assert reason is not None
    assert "unexpected keyword argument" in reason.lower()
    assert "takes no keyword arguments" not in reason.lower(), (
        "higher-precedence signal must win"
    )


def test_precedence_takes_no_keyword_arguments_over_cannot_import_name():
    evidence = "takes no keyword arguments; cannot import name 'X'"
    reason = rebrief.detect_unsatisfiable_signal(evidence)
    assert reason is not None
    assert "takes no keyword arguments" in reason.lower()
    assert "cannot import name" not in reason.lower()


def test_precedence_cannot_import_name_over_has_no_attribute():
    evidence = "cannot import name 'X' but also has no attribute 'Y'"
    reason = rebrief.detect_unsatisfiable_signal(evidence)
    assert reason is not None
    assert "cannot import name" in reason.lower()
    assert "has no attribute" not in reason.lower()


def test_precedence_has_no_attribute_over_is_not_defined():
    evidence = "has no attribute 'Y' and name 'Z' is not defined"
    reason = rebrief.detect_unsatisfiable_signal(evidence)
    assert reason is not None
    assert "has no attribute" in reason.lower()
    assert "is not defined" not in reason.lower()


def test_precedence_top_signal_wins_over_all_lower_ones():
    evidence = (
        "got an unexpected keyword argument 'a'; takes no keyword arguments; "
        "cannot import name 'b'; has no attribute 'c'; is not defined"
    )
    reason = rebrief.detect_unsatisfiable_signal(evidence)
    assert reason is not None
    assert "unexpected keyword argument" in reason.lower()
    for lower in (
        "takes no keyword arguments",
        "cannot import name",
        "has no attribute",
        "is not defined",
    ):
        assert lower not in reason.lower(), f"lower signal {lower!r} must not appear"


# ---------------------------------------------------------------------------
# Never-raises contract
# ---------------------------------------------------------------------------

def test_never_raises_for_a_variety_of_unusual_inputs():
    weird_inputs = [
        None,
        "",
        "   ",
        0,
        1,
        -1,
        3.14,
        True,
        False,
        [],
        {},
        (),
        object(),
        b"bytes here",
        complex(1, 2),
    ]
    for value in weird_inputs:
        # Must not raise for ANY input.
        result = rebrief.detect_unsatisfiable_signal(value)
        # Non-string inputs return None; we don't assert on string inputs here.
        if not isinstance(value, str):
            assert result is None, f"non-string {value!r} must return None, got {result!r}"


def test_does_not_raise_on_very_long_input():
    evidence = "got an unexpected keyword argument 'x'\n" + ("x" * 1_000_000)
    reason = rebrief.detect_unsatisfiable_signal(evidence)
    assert reason is not None
    assert "unexpected keyword argument" in reason.lower()
    assert len(reason) <= 200


# ---------------------------------------------------------------------------
# Reason is short and human-readable
# ---------------------------------------------------------------------------

def test_reason_for_unexpected_keyword_argument_is_short_and_descriptive():
    reason = rebrief.detect_unsatisfiable_signal(
        "got an unexpected keyword argument 'limit'"
    )
    assert reason is not None
    assert isinstance(reason, str)
    assert len(reason) <= 200
    assert "unexpected keyword argument" in reason.lower()