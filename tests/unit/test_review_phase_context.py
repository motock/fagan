"""Recorded external context is injected into the reviewer and security reviewer prompts."""
from __future__ import annotations

import pytest

import pipeline.review as review_mod
import pipeline.server  # noqa: F401
from pipeline import context_sources as ctxsrc
from pipeline import paths

NAME = "ux"
TEXT = "Always use the shared Button component."


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PLAN_DIR", tmp_path)
    return tmp_path


def _manifest(digest, brief="Do the thing.", phases=("dispatch", "review", "security_review")):
    story = {"summary": "S", "status": "in_progress", "agent_instructions": brief}
    if digest:
        story["context_digests"] = {NAME: {"digest": digest, "bytes": len(TEXT)}}
    source = {
        "name": NAME, "provider": "dir", "uri": "/x", "mode": "prompt",
        "authority": "reference", "phases": list(phases), "required": True,
    }
    return {"context": {"sources": [source]}, "stories": {"CL-1": story}}


def test_should_include_context_block_in_review_context(plan_dir):
    digest = ctxsrc.snapshot_context(TEXT)
    out = review_mod.build_review_story_context("CL-1", _manifest(digest))
    assert TEXT in out
    assert out.index("Do the thing.") < out.index(TEXT) < out.index(review_mod.REVIEW_CONTEXT_SCOPE_RULE)


def test_should_warn_when_snapshot_missing(plan_dir):
    digest = "sha256:" + "ab" * 32
    out = review_mod.build_review_story_context("CL-1", _manifest(digest))
    assert f"context source {NAME}" in out
    assert "cannot verify" in out


def test_should_warn_when_snapshot_digest_mismatches(plan_dir):
    digest = ctxsrc.snapshot_context(TEXT)
    (plan_dir / "context" / digest.removeprefix("sha256:") / "content.txt").write_text("tampered")
    out = review_mod.build_review_story_context("CL-1", _manifest(digest))
    assert "cannot verify" in out
    assert "tampered" not in out


def test_should_return_context_block_when_brief_is_absent(plan_dir):
    digest = ctxsrc.snapshot_context(TEXT)
    out = review_mod.build_review_story_context("CL-1", _manifest(digest, brief="  "))
    assert TEXT in out
    assert review_mod.REVIEW_CONTEXT_SCOPE_RULE in out


def test_should_return_none_when_brief_blank_and_no_context(plan_dir):
    assert review_mod.build_review_story_context("CL-1", _manifest(None, brief="  ")) is None


def test_should_omit_sources_not_scoped_to_review(plan_dir):
    digest = ctxsrc.snapshot_context(TEXT)
    out = review_mod.build_review_story_context("CL-1", _manifest(digest, phases=("dispatch",)))
    assert TEXT not in out


def test_should_build_security_review_block_only_for_that_phase(plan_dir):
    digest = ctxsrc.snapshot_context(TEXT)
    manifest = _manifest(digest, phases=("security_review",))
    assert TEXT in review_mod.build_phase_context_block("CL-1", manifest, "security_review")
    assert review_mod.build_phase_context_block("CL-1", manifest, "review") == ""


def test_security_reviewer_prompt_includes_context_block(monkeypatch):
    captured = {}

    class FakeBackend:
        def complete(self, prompt, **kw):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(review_mod.backend, "get_backend", lambda *a, **k: FakeBackend())
    monkeypatch.setattr(review_mod, "_persona_body", lambda name: "persona")
    review_mod._run_security_reviewer("/tmp/wt", "agent/CL-1", context_block="CTX-BLOCK")
    assert captured["prompt"].endswith("CTX-BLOCK")


def test_security_reviewer_prompt_unchanged_without_context(monkeypatch):
    captured = {}

    class FakeBackend:
        def complete(self, prompt, **kw):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"

    monkeypatch.setattr(review_mod.backend, "get_backend", lambda *a, **k: FakeBackend())
    monkeypatch.setattr(review_mod, "_persona_body", lambda name: "persona")
    review_mod._run_security_reviewer("/tmp/wt", "agent/CL-1")
    assert captured["prompt"].endswith("APPROVE or REQUEST_CHANGES.")
