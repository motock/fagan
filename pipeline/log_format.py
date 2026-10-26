"""Opt-in structured (JSON) log format: ``PIPELINE_LOG_FORMAT=json``.

The default changes nothing: unset (or any other value) leaves the root
logger exactly as whatever configured it first left it — ``MCPServer``'s
constructor calls ``logging.basicConfig`` at import, uvicorn configures its
own handlers, and the daemon falls through to the last-resort stderr handler.

Why a formatter and not more ``basicConfig``: ``basicConfig`` is a no-op once a
handler exists, and by the time an entrypoint can opt in, one always does. The
opt-in path therefore installs its own handler and clears the others, so an
operator never sees each line twice in two formats.

Fields follow the project's logging standard — ``timestamp``, ``level``,
``service``, ``correlation_id``, ``message`` — plus every ``extra=`` key the
call site supplied. Dynamic values belong in fields rather than concatenated
into the message; this formatter is what makes that instruction true instead
of aspirational.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

from .actor import current_correlation_id

#: The opt-in switch. Only the exact value ``json`` (case and surrounding
#: whitespace ignored) turns the formatter on.
LOG_FORMAT_ENV = "PIPELINE_LOG_FORMAT"

#: Attributes ``logging`` puts on every record (plus the two ``Formatter``
#: adds). Everything else on the record came from a call site's ``extra=``.
_RESERVED_FIELDS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None))
) | frozenset({"message", "asctime"})


class JsonLogFormatter(logging.Formatter):
    """Render one log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as JSON, never raising on an odd field value."""
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": record.name,
            "correlation_id": current_correlation_id(),
            "message": record.getMessage(),
        }
        payload.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in _RESERVED_FIELDS
            }
        )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str: an extra= value the caller did not make JSON-safe must
        # degrade to its repr, never raise inside logging.
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging_from_env(environ: dict[str, str] | None = None) -> bool:
    """Install the JSON handler when the env var asks for it; else do nothing.

    Returns whether the JSON handler is installed. Idempotent: a second call
    with the handler already in place is a no-op. The root logger's *level* is
    left alone — this story changes how lines are rendered, not which lines
    are emitted.
    """
    env = os.environ if environ is None else environ
    if str(env.get(LOG_FORMAT_ENV, "")).strip().lower() != "json":
        return False
    root = logging.getLogger()
    if any(isinstance(handler.formatter, JsonLogFormatter) for handler in root.handlers):
        return True
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    # Replace, do not add: basicConfig/uvicorn already installed a plain-text
    # handler, and keeping it would print every line twice in two formats.
    root.handlers = [handler]
    return True
