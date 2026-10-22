"""TTY-safe prompt primitives for the guided installer.

Every function takes its I/O as injectable callables, so this module is
importable and testable without a real terminal: nothing here reads stdin,
writes stdout, or touches the filesystem at import time.

Fallback contract: a prompt that cannot read input (EOF, closed stream) never
raises - it returns the caller's documented default instead, so an unattended
install degrades to its defaults rather than hanging or crashing.

Secret contract: a value read through ``getpass_fn`` is never echoed, never
passed to ``output``, and never embedded in an exception message.
"""

from __future__ import annotations

import getpass
import os
import sys
from collections.abc import Callable, Sequence

__all__ = ["ask_yes_no", "is_interactive", "prompt_value"]

#: Prompt kinds ``prompt_value`` understands. Named in its ValueError so a
#: caller that typos a kind learns the supported set.
_SUPPORTED_KINDS: tuple[str, ...] = ("text", "choice", "secret")

#: Failures a stream's ``isatty`` may raise when the descriptor is gone.
_TTY_ERRORS = (AttributeError, OSError, ValueError)


def _is_tty(stream: object, fd: int) -> bool:
    """Return whether ``stream`` is a TTY, falling back to ``os.isatty(fd)``.

    The fallback is consulted only when the stream has no ``isatty`` at all;
    a stream that has one and raises is treated as non-interactive rather than
    second-guessed.
    """
    try:
        isatty = getattr(stream, "isatty", None)
    except _TTY_ERRORS:
        return False
    if isatty is None:
        try:
            return bool(os.isatty(fd))
        except _TTY_ERRORS:
            return False
    try:
        return bool(isatty())
    except _TTY_ERRORS:
        return False


def is_interactive(stdin: object = None, stdout: object = None) -> bool:
    """Return True only when both streams are TTYs.

    Defaults are ``sys.stdin`` and ``sys.stdout``. Any AttributeError, OSError
    or ValueError raised by either check makes the answer False: an
    unanswerable stream is never assumed interactive.
    """
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    return _is_tty(stdin, 0) and _is_tty(stdout, 1)


def ask_yes_no(
    question: str,
    *,
    default: bool,
    input_fn: Callable[[], str] = input,
    output: Callable[[str], None] = print,
) -> bool:
    """Ask a yes/no question, returning ``default`` when input is unavailable.

    The hint reflects the default (``(Y/n)`` or ``(y/N)``). Empty input takes
    the default; ``y``/``yes`` and ``n``/``no`` are accepted case-insensitively
    and stripped; anything else prints a one-line retry and asks again. EOF and
    Ctrl-C both return ``default`` instead of escaping.
    """
    hint = "(Y/n)" if default else "(y/N)"
    prompt = f"{question} {hint}"
    while True:
        output(prompt)
        try:
            raw = input_fn()
        except (EOFError, KeyboardInterrupt):
            return default
        answer = str(raw).strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        output("Please answer 'y' or 'n'.")


def _format_choices(choices: Sequence[str]) -> str:
    """Render a numbered, 1-based list of ``choices``."""
    return "\n".join(f"{index}. {choice}" for index, choice in enumerate(choices, start=1))


def _prompt_text(
    label: str,
    default: str | None,
    validate: Callable[[str], tuple[bool, str]] | None,
    input_fn: Callable[[], str],
    output: Callable[[str], None],
) -> str | None:
    """Free-text prompt: empty input takes ``default``, else ``validate`` runs."""
    prompt = label if default is None else f"{label} [{default}]"
    while True:
        output(prompt)
        try:
            raw = input_fn()
        except EOFError:
            return default
        if str(raw).strip() == "":
            return default
        if validate is None:
            return raw
        ok, result = validate(raw)
        if ok:
            return result
        output(str(result))


def _prompt_choice(
    label: str,
    default: str | None,
    choices: Sequence[str],
    input_fn: Callable[[], str],
    output: Callable[[str], None],
) -> str | None:
    """Choice prompt: accepts a 1-based index or an exact choice string."""
    if not choices:
        raise ValueError("choice prompts require a non-empty tuple of choices")
    if default is not None and default not in choices:
        raise ValueError(f"default {default!r} is not one of the available choices")
    prompt = label if default is None else f"{label} [{default}]"
    while True:
        output(prompt)
        try:
            raw = input_fn()
        except EOFError:
            return default
        answer = str(raw).strip()
        if not answer:
            return default
        if answer in choices:
            return answer
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(choices):
                return choices[index - 1]
        output(_format_choices(choices))


def _prompt_secret(
    label: str,
    getpass_fn: Callable[[], str],
    output: Callable[[str], None],
) -> str | None:
    """Secret prompt: never echoes the value, never prints a default."""
    output(f"{label}:")
    try:
        value = getpass_fn()
    except Exception:  # noqa: BLE001 - no terminal / EOF must degrade to None
        return None
    return value or None


def prompt_value(
    label: str,
    *,
    kind: str,
    default: str | None = None,
    choices: Sequence[str] = (),
    validate: Callable[[str], tuple[bool, str]] | None = None,
    input_fn: Callable[[], str] = input,
    output: Callable[[str], None] = print,
    getpass_fn: Callable[[], str] = getpass.getpass,
) -> str | None:
    """Prompt for a value of ``kind`` (``text``, ``choice`` or ``secret``).

    Returns the entered (or default) value, or None when a secret prompt has
    nothing to read. Raises ValueError for an unknown ``kind``, an empty
    ``choices`` sequence, or a ``default`` outside ``choices``.
    """
    if kind == "text":
        return _prompt_text(label, default, validate, input_fn, output)
    if kind == "choice":
        return _prompt_choice(label, default, choices, input_fn, output)
    if kind == "secret":
        return _prompt_secret(label, getpass_fn, output)
    supported = ", ".join(_SUPPORTED_KINDS)
    raise ValueError(f"unsupported prompt kind {kind!r}; supported kinds: {supported}")
