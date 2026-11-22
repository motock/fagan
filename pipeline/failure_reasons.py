"""Failure-reason wording for a story grade that ends `failed`.

The grade path decides when a reason applies; this module owns the text.
Nothing here copies agent or test output into a reason: the auth hint is a
fixed sentence, added only when a fixed marker shows up in the tail of the
agent log.
"""

import os

TEST_NOT_FOUND_RETURNCODE = 127
AUTH_FAILURE_MARKERS = (
    "not logged in",
    "401 unauthorized",
    "invalid api key",
    "authentication_error",
)
AUTH_FAILURE_SENTENCE = " The agent could not authenticate to its model provider."
LOG_TAIL_BYTES = 4000


def tests_failed_reason(returncode: int) -> str:
    if returncode == TEST_NOT_FOUND_RETURNCODE:
        return "test command not found on the pipeline host (exit 127)"
    return f"tests failed (exit {returncode})"


def auth_failure_hint(log_path) -> str:
    """Return the auth sentence when the log's tail shows an auth failure."""
    if not log_path:
        return ""
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - LOG_TAIL_BYTES))
            tail = fh.read(LOG_TAIL_BYTES)
    except OSError:
        return ""
    text = tail.decode("utf-8", errors="replace").lower()
    if any(marker in text for marker in AUTH_FAILURE_MARKERS):
        return AUTH_FAILURE_SENTENCE
    return ""
