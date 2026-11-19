"""Snapshot of what the overlord was shown when it ruled, so a ruling can be replayed.

Each ``failure triage`` and ``high-risk merge adjudication`` record carries an
``input`` object built here. It holds the redacted, capped question part of the
prompt and digests of the prompt and policy text - never the raw prompt, so a
token or secret cannot reach the decisions log through this path.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from app import role_registry

from .persona import _persona_default_model
from .story_evidence import redact

#: Cap on the redacted evidence kept per ruling. The console polls decisions,
#: so the snapshot is bounded; ``truncated`` says when the cap bit.
EVIDENCE_MAX_CHARS = 16000


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _overlord_backend_and_model(plan_role_config: dict | None) -> tuple[str | None, str | None]:
    """The overlord's backend and model, as ``_invoke_overlord`` resolves them.

    Never raises: a snapshot must not break the ruling it describes, so an
    unresolvable overlord is recorded as ``None`` for both.
    """
    try:
        resolution = role_registry.resolve_role(
            "overlord",
            plan_role_config=plan_role_config,
            model_fallback=lambda: _persona_default_model("overlord") or "opus",
        )
    except Exception as exc:  # noqa: BLE001 - the snapshot degrades, the ruling does not
        logging.getLogger("pipeline").warning(
            "ruling snapshot: overlord resolution unavailable (%s)", type(exc).__name__
        )
        return None, None
    return resolution.provider, resolution.model


def ruling_input(
    *,
    evidence: str,
    prompt: str | None,
    policy: str | None,
    context_digests: list[str] | None,
    park_class: str,
    plan_role_config: dict | None,
) -> dict[str, Any]:
    """Describe what the overlord was shown for one ruling.

    ``evidence`` is the question part of the prompt - everything except the
    policy text - so a replay can rebuild the prompt as ``policy + evidence``.
    """
    redacted = redact(evidence)
    backend, model = _overlord_backend_and_model(plan_role_config)
    return {
        "evidence": redacted[:EVIDENCE_MAX_CHARS],
        "truncated": len(redacted) > EVIDENCE_MAX_CHARS,
        "prompt_sha256": None if prompt is None else _sha256(prompt),
        "policy_sha256": None if policy is None else _sha256(policy),
        "context_digests": list(context_digests) if context_digests is not None else [],
        "backend": backend,
        "model": model,
        "park_class": park_class,
    }
