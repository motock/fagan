"""Unit tests for the TTY-safe prompt primitives in ``pipeline.setup.ui``.

Every test injects fake streams and callables, so the suite never touches a
real terminal, stdin, or stdout.
"""

from __future__ import annotations

import pytest

from pipeline.setup import ui

SENTINEL = "s3cr3t-sentinel"


class FakeStream:
    """A stream whose ``isatty`` answer is fixed."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class RaisingStream:
    """A stream whose ``isatty`` raises, like a closed descriptor."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def isatty(self) -> bool:
        raise self._exc


class NoIsattyStream:
    """A stream without an ``isatty`` attribute at all."""


class FakeInput:
    """Scripted replacement for ``input``.

    Each answer is returned in turn; an exhausted script raises ``EOFError``.
    Exception instances in the script are raised instead of returned.
    """

    def __init__(self, answers) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        value = self.answers.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class FakeOutput:
    """Captures every message passed to the output callable."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, message: str = "") -> None:
        self.calls.append(message)

    def text(self) -> str:
        return "\n".join(str(call) for call in self.calls)


# ---------------------------------------------------------------------------
# is_interactive
# ---------------------------------------------------------------------------


def test_is_interactive_true_only_when_both_streams_are_ttys() -> None:
    assert ui.is_interactive(FakeStream(True), FakeStream(True)) is True


def test_is_interactive_false_when_only_stdin_is_a_tty() -> None:
    assert ui.is_interactive(FakeStream(True), FakeStream(False)) is False


def test_is_interactive_false_when_only_stdout_is_a_tty() -> None:
    assert ui.is_interactive(FakeStream(False), FakeStream(True)) is False


@pytest.mark.parametrize("exc", [AttributeError("gone"), OSError("closed"), ValueError("bad")])
def test_is_interactive_false_when_isatty_raises(exc: BaseException) -> None:
    assert ui.is_interactive(RaisingStream(exc), FakeStream(True)) is False
    assert ui.is_interactive(FakeStream(True), RaisingStream(exc)) is False


def test_is_interactive_falls_back_to_os_isatty_when_stream_lacks_isatty(monkeypatch) -> None:
    monkeypatch.setattr(ui.os, "isatty", lambda fd: False)
    assert ui.is_interactive(NoIsattyStream(), NoIsattyStream()) is False


def test_is_interactive_fallback_is_unanimous(monkeypatch) -> None:
    monkeypatch.setattr(ui.os, "isatty", lambda fd: True)
    assert ui.is_interactive(NoIsattyStream(), NoIsattyStream()) is True
    # A stream that reports a TTY plus a fallback TTY is still interactive.
    assert ui.is_interactive(FakeStream(True), NoIsattyStream()) is True


def test_is_interactive_fallback_os_isatty_raising_is_false(monkeypatch) -> None:
    def boom(fd: int) -> bool:
        raise OSError("no descriptor")

    monkeypatch.setattr(ui.os, "isatty", boom)
    assert ui.is_interactive(NoIsattyStream(), NoIsattyStream()) is False


# ---------------------------------------------------------------------------
# ask_yes_no
# ---------------------------------------------------------------------------


def test_ask_yes_no_returns_default_on_empty_input() -> None:
    assert ui.ask_yes_no("Continue?", default=True, input_fn=FakeInput([""]), output=FakeOutput()) is True
    assert ui.ask_yes_no("Continue?", default=False, input_fn=FakeInput([""]), output=FakeOutput()) is False


@pytest.mark.parametrize("answer", ["y", "Y", "YES", " yes "])
def test_ask_yes_no_affirmative_answers(answer: str) -> None:
    assert ui.ask_yes_no("Continue?", default=False, input_fn=FakeInput([answer]), output=FakeOutput()) is True


@pytest.mark.parametrize("answer", ["n", "N", "NO", " no "])
def test_ask_yes_no_negative_answers(answer: str) -> None:
    assert ui.ask_yes_no("Continue?", default=True, input_fn=FakeInput([answer]), output=FakeOutput()) is False


def test_ask_yes_no_hint_reflects_default() -> None:
    yes_out = FakeOutput()
    ui.ask_yes_no("Continue?", default=True, input_fn=FakeInput([""]), output=yes_out)
    assert "(Y/n)" in yes_out.text()

    no_out = FakeOutput()
    ui.ask_yes_no("Continue?", default=False, input_fn=FakeInput([""]), output=no_out)
    assert "(y/N)" in no_out.text()


def test_ask_yes_no_reasks_after_invalid_answer() -> None:
    fake_input = FakeInput(["maybe", "y"])
    out = FakeOutput()
    assert ui.ask_yes_no("Continue?", default=False, input_fn=fake_input, output=out) is True
    assert len(fake_input.prompts) == 2
    assert len(out.calls) >= 1


def test_ask_yes_no_eof_returns_default() -> None:
    assert ui.ask_yes_no("Continue?", default=True, input_fn=FakeInput([]), output=FakeOutput()) is True
    assert ui.ask_yes_no("Continue?", default=False, input_fn=FakeInput([]), output=FakeOutput()) is False


def test_ask_yes_no_keyboard_interrupt_returns_default() -> None:
    assert (
        ui.ask_yes_no(
            "Continue?", default=True, input_fn=FakeInput([KeyboardInterrupt()]), output=FakeOutput()
        )
        is True
    )


def test_ask_yes_no_question_with_braces_and_percent_is_literal() -> None:
    out = FakeOutput()
    question = "Install to {path} at 100%?"
    ui.ask_yes_no(question, default=True, input_fn=FakeInput([""]), output=out)
    assert question in out.text()


# ---------------------------------------------------------------------------
# prompt_value: text
# ---------------------------------------------------------------------------


def test_prompt_value_text_returns_default_on_empty() -> None:
    assert (
        ui.prompt_value("Name", kind="text", default="bob", input_fn=FakeInput([""]), output=FakeOutput())
        == "bob"
    )


def test_prompt_value_text_returns_normalized_value_from_validate() -> None:
    def validate(raw: str):
        return True, raw.strip().lower()

    result = ui.prompt_value(
        "Name", kind="text", validate=validate, input_fn=FakeInput(["  Bob "]), output=FakeOutput()
    )
    assert result == "bob"


def test_prompt_value_text_reasks_after_validation_failure() -> None:
    seen: list[str] = []

    def validate(raw: str):
        seen.append(raw)
        if raw == "bad":
            return False, "must be good"
        return True, raw

    fake_input = FakeInput(["bad", "good"])
    out = FakeOutput()
    result = ui.prompt_value("Name", kind="text", validate=validate, input_fn=fake_input, output=out)
    assert result == "good"
    assert seen == ["bad", "good"]
    assert "must be good" in out.text()


def test_prompt_value_text_eof_returns_default() -> None:
    assert (
        ui.prompt_value("Name", kind="text", default="fallback", input_fn=FakeInput([]), output=FakeOutput())
        == "fallback"
    )


def test_prompt_value_text_label_with_braces_and_percent_is_literal() -> None:
    out = FakeOutput()
    label = "Path {name} 100% done"
    ui.prompt_value(label, kind="text", input_fn=FakeInput([""]), output=out)
    assert label in out.text()


# ---------------------------------------------------------------------------
# prompt_value: choice
# ---------------------------------------------------------------------------


def test_prompt_value_choice_accepts_index_and_exact_name() -> None:
    choices = ("alpha", "beta")
    assert (
        ui.prompt_value("Pick", kind="choice", choices=choices, input_fn=FakeInput(["1"]), output=FakeOutput())
        == "alpha"
    )
    assert (
        ui.prompt_value("Pick", kind="choice", choices=choices, input_fn=FakeInput([" 2 "]), output=FakeOutput())
        == "beta"
    )
    assert (
        ui.prompt_value("Pick", kind="choice", choices=choices, input_fn=FakeInput(["beta"]), output=FakeOutput())
        == "beta"
    )


def test_prompt_value_choice_invalid_index_reasks_then_accepts() -> None:
    fake_input = FakeInput(["0", "99", "2"])
    out = FakeOutput()
    result = ui.prompt_value("Pick", kind="choice", choices=("alpha", "beta"), input_fn=fake_input, output=out)
    assert result == "beta"
    assert len(fake_input.prompts) == 3
    # The numbered list is printed when an answer is rejected.
    assert "1. alpha" in out.text()


def test_prompt_value_choice_empty_returns_default() -> None:
    assert (
        ui.prompt_value(
            "Pick",
            kind="choice",
            choices=("a", "b"),
            default="b",
            input_fn=FakeInput([""]),
            output=FakeOutput(),
        )
        == "b"
    )


def test_prompt_value_choice_eof_returns_default() -> None:
    assert (
        ui.prompt_value(
            "Pick",
            kind="choice",
            choices=("a", "b"),
            default="a",
            input_fn=FakeInput([]),
            output=FakeOutput(),
        )
        == "a"
    )


def test_prompt_value_choice_empty_choices_raises_value_error() -> None:
    with pytest.raises(ValueError):
        ui.prompt_value("Pick", kind="choice", choices=(), input_fn=FakeInput(["1"]), output=FakeOutput())


def test_prompt_value_choice_default_outside_choices_raises_value_error() -> None:
    with pytest.raises(ValueError):
        ui.prompt_value(
            "Pick",
            kind="choice",
            choices=("a", "b"),
            default="z",
            input_fn=FakeInput(["1"]),
            output=FakeOutput(),
        )


# ---------------------------------------------------------------------------
# prompt_value: secret
# ---------------------------------------------------------------------------


def test_prompt_value_secret_returns_getpass_value_without_echoing() -> None:
    out = FakeOutput()
    prompts: list[str] = []

    def fake_getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        return SENTINEL

    result = ui.prompt_value("Token", kind="secret", getpass_fn=fake_getpass, output=out)
    assert result == SENTINEL
    assert SENTINEL not in out.text()
    assert all(SENTINEL not in prompt for prompt in prompts)


def test_prompt_value_secret_empty_returns_none() -> None:
    assert ui.prompt_value("Token", kind="secret", getpass_fn=lambda prompt="": "", output=FakeOutput()) is None


def test_prompt_value_secret_getpass_raising_returns_none() -> None:
    def boom(prompt: str = "") -> str:
        raise EOFError

    assert ui.prompt_value("Token", kind="secret", getpass_fn=boom, output=FakeOutput()) is None


def test_prompt_value_secret_never_prints_default() -> None:
    out = FakeOutput()
    ui.prompt_value(
        "Token",
        kind="secret",
        default=SENTINEL,
        getpass_fn=lambda prompt="": "",
        output=out,
    )
    assert SENTINEL not in out.text()


def test_prompt_value_secret_sentinel_never_appears_in_output() -> None:
    out = FakeOutput()
    ui.prompt_value("Token", kind="secret", getpass_fn=lambda prompt="": SENTINEL, output=out)
    assert all(SENTINEL not in str(call) for call in out.calls)


# ---------------------------------------------------------------------------
# prompt_value: unknown kind
# ---------------------------------------------------------------------------


def test_prompt_value_unknown_kind_raises_value_error_naming_supported_kinds() -> None:
    with pytest.raises(ValueError) as excinfo:
        ui.prompt_value("X", kind="bogus", input_fn=FakeInput(["a"]), output=FakeOutput())
    message = str(excinfo.value)
    assert "text" in message
    assert "choice" in message
    assert "secret" in message
