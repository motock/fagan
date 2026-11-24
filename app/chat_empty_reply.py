"""Empty model answers in a chat turn (cer-01).

A model answer with no text is not a reply. ``app/chat.py`` nudges the model
once to answer in prose; if the answer is still empty, or no turn is left to
ask in, the turn ends with ``EMPTY_REPLY_TEXT`` and one ``chat reply empty``
warning. The warning carries the provider, model tag and turn count only.
"""
from __future__ import annotations

import logging

EMPTY_REPLY_TEXT = "The model returned no answer. Try sending the message again."

EMPTY_REPLY_NUDGE = (
    "Your previous response contained no text. Answer the person now "
    "in plain language, using the tool results you already have. "
    "Do not call another tool."
)


def log_empty_reply(provider: str, model: str, turns: int) -> None:
    logging.getLogger("pipeline").warning(
        "chat reply empty",
        extra={"provider": provider, "model": model, "turns": turns},
    )
