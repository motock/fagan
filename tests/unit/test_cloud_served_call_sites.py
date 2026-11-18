"""Call-site coverage for app.cloud_served (hpc-03).

Each place that used to key "is this model cloud-served?" off the ``:cloud``
suffix alone now delegates to :func:`app.cloud_served.is_cloud_served`, and
each of those call sites needs the same three-case proof:

* a plain-named tag on ``https://ollama.com`` is treated as cloud-served;
* the same tag on a localhost endpoint keeps today's on-device treatment;
* the ``:cloud`` suffix still wins on a localhost endpoint (the survivor).

Call sites: ``_tuned_num_ctx``, the dispatch step cap, the ``resource_status``
free-memory floor, ``pipeline.strength_class`` and
``pipeline.local_success.classify_story``.
"""

from __future__ import annotations

import pytest

from app import backend as b
from app import backend_ollama as bo
from app.ollama_prompt_utils import _tuned_num_ctx
from pipeline.local_success import classify_story
from pipeline.strength_class import (
    CAPABLE,
    ON_DEVICE,
    dispatch_strength_class,
    strength_class,
)
from tests.unit._backend_helpers import _FakePopenResult, _FakeResponse

HOSTED = "https://ollama.com"
LOCAL = "http://localhost:11434"
PLAIN = "glm-5.2"
CLOUD = "glm-5.2:cloud"


# ---------------------------------------------------------------------------
# call site 1: _tuned_num_ctx (wired through all three driver callers)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("tag", "endpoint", "expected"),
    [
        (PLAIN, HOSTED, 131072),  # plain name, hosted endpoint -> cloud knob
        (PLAIN, LOCAL, 32768),  # plain name, local endpoint -> on-device knob
        (CLOUD, LOCAL, 131072),  # the :cloud survivor, still honoured
    ],
)
def test_tuned_num_ctx_classifies_by_endpoint(monkeypatch, tag, endpoint, expected):
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "32768")
    monkeypatch.delenv("PIPELINE_CLOUD_NUM_CTX", raising=False)

    assert _tuned_num_ctx(tag, 16384, endpoint=endpoint) == expected


def test_dispatch_threads_its_endpoint_into_num_ctx(tmp_path, monkeypatch):
    """The driver's own endpoint reaches _tuned_num_ctx (caller wiring)."""
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", HOSTED)
    monkeypatch.setenv("PIPELINE_LOCAL_NUM_CTX", "32768")
    monkeypatch.delenv("PIPELINE_CLOUD_NUM_CTX", raising=False)
    monkeypatch.delenv("PIPELINE_LOCAL_MAX_STEPS", raising=False)

    driver = b.OllamaDriver()
    captured = {}
    monkeypatch.setattr(
        b.subprocess,
        "Popen",
        lambda argv, cwd, env, stdout, stderr: (
            captured.update(env=env) or _FakePopenResult(303)
        ),
    )

    driver.dispatch(
        "do it",
        system=None,
        model=PLAIN,
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path,
        log_path=tmp_path / "agent.log",
        append=False,
    )

    assert captured["env"]["PIPELINE_TRANSPORT_NUM_CTX"] == "131072"


# ---------------------------------------------------------------------------
# call site 2: the dispatch step cap + park-guard relaxation
# ---------------------------------------------------------------------------
def _dispatch_env(tmp_path, monkeypatch, endpoint, model):
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", endpoint)
    monkeypatch.delenv("PIPELINE_LOCAL_MAX_STEPS", raising=False)
    monkeypatch.delenv("PIPELINE_CLOUD_MAX_STEPS", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_PARK_ENABLED", raising=False)

    driver = b.OllamaDriver()
    captured = {}
    monkeypatch.setattr(
        b.subprocess,
        "Popen",
        lambda argv, cwd, env, stdout, stderr: (
            captured.update(env=env) or _FakePopenResult(303)
        ),
    )
    driver.dispatch(
        "do it",
        system=None,
        model=model,
        allowed_tools="Bash,Edit,Write,Read",
        cwd=tmp_path,
        log_path=tmp_path / "agent.log",
        append=False,
    )
    return captured["env"]


def test_dispatch_relaxes_step_cap_for_plain_name_on_hosted_endpoint(
    tmp_path, monkeypatch
):
    env = _dispatch_env(tmp_path, monkeypatch, HOSTED, PLAIN)

    assert env["PIPELINE_TRANSPORT_MAX_STEPS"] == "120"
    assert env["LOCAL_AGENT_PARK_ENABLED"] == "0"


def test_dispatch_keeps_step_cap_for_plain_name_on_localhost(tmp_path, monkeypatch):
    env = _dispatch_env(tmp_path, monkeypatch, LOCAL, PLAIN)

    assert env["PIPELINE_TRANSPORT_MAX_STEPS"] == "40"
    assert "LOCAL_AGENT_PARK_ENABLED" not in env


def test_dispatch_still_relaxes_step_cap_for_cloud_suffix_on_localhost(
    tmp_path, monkeypatch
):
    env = _dispatch_env(tmp_path, monkeypatch, LOCAL, CLOUD)

    assert env["PIPELINE_TRANSPORT_MAX_STEPS"] == "120"
    assert env["LOCAL_AGENT_PARK_ENABLED"] == "0"


# ---------------------------------------------------------------------------
# call site 3: the resource_status free-memory floor
# ---------------------------------------------------------------------------
def _resource_status(monkeypatch, endpoint, tag):
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", endpoint)
    monkeypatch.setenv("PIPELINE_LOCAL_MIN_FREE_MEMORY_MB", "2048")
    monkeypatch.setattr(b.httpx, "get", lambda url, timeout: _FakeResponse({}))
    # Weights gate is orthogonal to the floor; 0 (a cloud-served footprint)
    # makes it fail open so the assertion is about the floor only.
    monkeypatch.setattr(bo, "_ollama_model_weights_mb", lambda endpoint, tag: 0)
    driver = b.OllamaDriver()
    monkeypatch.setattr(driver, "_free_memory_mb", lambda: 512)
    return driver.resource_status(tag)


def test_memory_floor_skipped_for_plain_name_on_hosted_endpoint(monkeypatch):
    status = _resource_status(monkeypatch, HOSTED, PLAIN)

    assert status["ok"] is True


def test_memory_floor_applies_to_plain_name_on_localhost(monkeypatch):
    status = _resource_status(monkeypatch, LOCAL, PLAIN)

    assert status["ok"] is False
    assert "insufficient free memory" in status["reason"]


def test_memory_floor_still_skipped_for_cloud_suffix_on_localhost(monkeypatch):
    status = _resource_status(monkeypatch, LOCAL, CLOUD)

    assert status["ok"] is True


# ---------------------------------------------------------------------------
# call site 4: pipeline.strength_class
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("tag", "endpoint", "expected"),
    [
        (PLAIN, HOSTED, CAPABLE),
        (PLAIN, LOCAL, ON_DEVICE),
        (CLOUD, LOCAL, CAPABLE),
        # endpoint omitted entirely: today's rule, i.e. on-device
        (PLAIN, None, ON_DEVICE),
    ],
)
def test_strength_class_uses_endpoint_when_supplied(tag, endpoint, expected):
    assert strength_class("ollama", tag, endpoint=endpoint) == expected


def test_dispatch_strength_class_reads_endpoint_from_env(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT_OLLAMA", HOSTED)
    monkeypatch.delenv("PIPELINE_LOCAL_ENDPOINT", raising=False)

    assert (
        dispatch_strength_class(
            {"backend": "ollama", "model": PLAIN}, None, registry={}, environ=None
        )
        == CAPABLE
    )


def test_dispatch_strength_class_localhost_keeps_plain_name_on_device(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", LOCAL)
    monkeypatch.delenv("PIPELINE_LOCAL_ENDPOINT_OLLAMA", raising=False)

    assert (
        dispatch_strength_class(
            {"backend": "ollama", "model": PLAIN}, None, registry={}, environ=None
        )
        == ON_DEVICE
    )


def test_dispatch_strength_class_cloud_suffix_survives_localhost(monkeypatch):
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", LOCAL)
    monkeypatch.delenv("PIPELINE_LOCAL_ENDPOINT_OLLAMA", raising=False)

    assert (
        dispatch_strength_class(
            {"backend": "ollama", "model": CLOUD}, None, registry={}, environ=None
        )
        == CAPABLE
    )


# ---------------------------------------------------------------------------
# call site 5: pipeline.local_success tier attribution
# ---------------------------------------------------------------------------
def _story(tag):
    return {
        "story_key": "S1",
        "status": "done",
        "backend": "local",
        "model": tag,
        "dispatched_model": tag,
        "dispatched_at": "2026-09-18T10:00:00Z",
    }


@pytest.mark.parametrize(
    ("tag", "endpoint", "expected"),
    [
        (PLAIN, HOSTED, "cloud-oss"),
        (PLAIN, LOCAL, "on-device"),
        (CLOUD, LOCAL, "cloud-oss"),
    ],
)
def test_local_success_tier_uses_endpoint(monkeypatch, tag, endpoint, expected):
    monkeypatch.setenv("PIPELINE_LOCAL_ENDPOINT", endpoint)
    monkeypatch.delenv("PIPELINE_LOCAL_ENDPOINT_OLLAMA", raising=False)

    assert classify_story("S1", _story(tag), [])["tier"] == expected
