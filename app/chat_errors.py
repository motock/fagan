"""Structured refusals for a chat turn that cannot produce a reply.

A failed model call or an unconfigured chat role is answered with a reason
code, a fixed message and the request's correlation id. The raw error text is
kept server-side only: it can carry paths and provider output.
"""

from __future__ import annotations

import logging

from app.role_registry import RoleRegistryError

_logger = logging.getLogger("pipeline")

CHAT_BACKEND_FAILED = "chat_backend_failed"
CHAT_NOT_CONFIGURED = "chat_not_configured"

_MESSAGES = {
    CHAT_BACKEND_FAILED: "The chat model could not be reached.",
    CHAT_NOT_CONFIGURED: "No chat model is configured for this team.",
}


class ChatBackendError(RuntimeError):
    """A chat model call failed. The raw error is kept as ``__cause__``."""

    def __init__(self, provider: str | None):
        super().__init__("chat model call failed")
        self.provider = provider


def is_chat_refusal(exc: BaseException) -> bool:
    """Return True when *exc* is a failure answered with a structured refusal."""
    return isinstance(exc, (ChatBackendError, RoleRegistryError))


def refuse(exc: BaseException, correlation_id: str) -> dict:
    """Log *exc* once at WARNING and return the refusal body for it.

    Logs carry only the reason, the error type, the provider and the
    correlation id: no prompt or message text, and no raw error text.
    """
    if isinstance(exc, ChatBackendError):
        reason = CHAT_BACKEND_FAILED
        provider = exc.provider
        error_type = type(exc.__cause__).__name__
    else:
        reason = CHAT_NOT_CONFIGURED
        provider = None
        error_type = type(exc).__name__
    _logger.warning(
        "chat turn refused",
        extra={
            "reason": reason,
            "error_type": error_type,
            "provider": provider,
            "correlation_id": correlation_id,
        },
    )
    return {"reason": reason, "message": _MESSAGES[reason], "correlation_id": correlation_id}
