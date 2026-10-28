"""Answer a service authorization refusal as HTTP 403 without naming the principal."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def raise_if_forbidden(result: Any) -> None:
    """Raise a 403 when *result* is an authorization refusal; otherwise return.

    The detail names the refused action only. The refusal's ``error`` text names
    the principal, which belongs in the server log and the audit stream, not in
    a response body.
    """
    if not isinstance(result, dict) or not result.get("forbidden"):
        return
    action = result.get("action")
    label = action if isinstance(action, str) and action else "this action"
    raise HTTPException(status_code=403, detail=f"not authorized: {label}")
