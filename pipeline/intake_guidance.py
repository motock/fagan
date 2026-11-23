"""Open questions from the decomposer, cleaned for a guidance record.

The product-analyst may add a top-level ``open_questions`` list beside a plan
when a request leaves a decision open. ``take_questions`` removes that key from
the plan, so the plan saved afterwards carries no unknown key, and returns the
questions cleaned for ``pipeline.plan_guidance.raise_guidance``.

The text comes from an outside ticket and is untrusted. A malformed value
yields no questions and never fails the intake: the key is removed either way.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from pipeline.plan_guidance import MAX_QUESTION_CHARS, MAX_QUESTIONS

OPEN_QUESTIONS_KEY = "open_questions"


def _strip_control(text: str) -> str:
    """Return ``text`` without Unicode control characters (category ``Cc``)."""
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cc")


def take_questions(plan: dict[str, Any]) -> list[str]:
    """Remove ``open_questions`` from ``plan`` and return its usable questions.

    Non-string entries and entries blank after cleaning are dropped. The result
    is capped at ``MAX_QUESTIONS`` and each question at ``MAX_QUESTION_CHARS``.
    """
    raw = plan.pop(OPEN_QUESTIONS_KEY, None)
    if not isinstance(raw, list):
        return []
    questions: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        text = _strip_control(entry).strip()[:MAX_QUESTION_CHARS].strip()
        if text:
            questions.append(text)
    return questions[:MAX_QUESTIONS]
