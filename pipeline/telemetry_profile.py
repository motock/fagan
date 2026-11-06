"""Deployment telemetry profile switch (``PIPELINE_TELEMETRY_PROFILE``).

The profile decides how much metadata a telemetry signal carries: ``customer``
carries low-cardinality, enum-bounded metadata only, ``harness`` adds per-attempt
metadata such as role, backend, model, attempt number and token counts. Content
— prompts, briefs, transcripts, diffs, file contents — is never an attribute, a
label or a span name on either profile (docs/TELEMETRY_CONTRACT.md,
``## Profiles`` and ``## Data classification``).

The profile is a DEPLOYMENT setting read from the environment. It is never
request data, a header, a plan field or a story field: no code path may let a
caller choose it. Exactly one value selects ``harness``; every unset, blank,
malformed or unknown value resolves to the restrictive ``customer`` profile.

The environment is read on every call and nothing is cached, so a deployment
that changes the variable takes effect on the next read. Production code reads
it once, at configure time.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

PROFILE_ENV = "PIPELINE_TELEMETRY_PROFILE"
CUSTOMER = "customer"
HARNESS = "harness"

_log = logging.getLogger(__name__)

# One warning per process is enough to point an operator at the bad variable;
# repeating it on every read would spam the log. The value is never logged: it
# may be arbitrary, untrusted input.
_warned_unknown = False


def profile(environ: Mapping[str, str] | None = None) -> str:
    """Return the deployment telemetry profile: ``harness`` or ``customer``."""
    global _warned_unknown

    env = os.environ if environ is None else environ
    raw = env.get(PROFILE_ENV)
    value = (raw or "").strip().lower()

    if value == HARNESS:
        return HARNESS
    if value and value != CUSTOMER and not _warned_unknown:
        _warned_unknown = True
        _log.warning(
            "Unrecognized %s value; falling back to the %s telemetry profile",
            PROFILE_ENV,
            CUSTOMER,
        )
    return CUSTOMER


def is_harness(environ: Mapping[str, str] | None = None) -> bool:
    """Return True only when the deployment resolved to the ``harness`` profile."""
    return profile(environ) == HARNESS
