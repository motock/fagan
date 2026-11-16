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

# The last word of a secret's name. It must be a whole word: "monkey" ends in
# "key" but the "key" is preceded by a letter, and "keyboard" starts with "key"
# but the "key" is followed by one, so neither is a secret name.
_WORDS = r"(?:TOKEN|SECRET|PASSWORD|API_KEY|KEY)"
# A hyphenated, dotted or underscored prefix such as "x-api-" or "openai_".
_PREFIX = r"[A-Za-z0-9_.\-]*?"
# Left boundary before the word, right boundary after it.
_NAME = rf"{_PREFIX}(?<![A-Za-z0-9]){_WORDS}(?![A-Za-z0-9_])"

_SECRET_NAME = re.compile(_NAME, re.IGNORECASE)
# name, then "=" / ":" (possibly quoted, as in JSON or Python literals), then value.
_NAMED = re.compile(
    rf"({_NAME})(\s*[\"']?\s*[=:]\s*[\"']?)(\"[^\"]*\"|'[^']*'|[^\s\"',}}]+)",
    re.IGNORECASE,
)


def is_secret_name(name: object) -> bool:
    """True when *name* itself names a secret, e.g. ``api_key`` or ``x-api-key``."""
    return isinstance(name, str) and _SECRET_NAME.fullmatch(name) is not None


def redact(text: str) -> str:
    """Replace secret values in *text* with ``[redacted]``, keeping names."""
    text = _PEM.sub(REDACTED, text)
    text = _TOKEN.sub(REDACTED, text)
    text = _BEARER.sub(lambda m: m.group(1) + REDACTED, text)
    return _NAMED.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)
