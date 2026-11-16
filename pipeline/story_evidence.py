"""Masking for story evidence (logs, journals) returned over read tools."""

import re

REDACTED = "[redacted]"

_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_TOKEN = re.compile(
    r"\b(?:gh[pos]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})"
)
_BEARER = re.compile(r"(\bBearer\s+)[^\s\"']+", re.IGNORECASE)
_NAMED = re.compile(
    r"\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|KEY))(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s\"']+)"
)


def redact(text: str) -> str:
    """Replace secret values in *text* with ``[redacted]``, keeping names."""
    text = _PEM.sub(REDACTED, text)
    text = _TOKEN.sub(REDACTED, text)
    text = _BEARER.sub(lambda m: m.group(1) + REDACTED, text)
    return _NAMED.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)
