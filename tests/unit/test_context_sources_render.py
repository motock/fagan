import pytest

from pipeline.context_sources import (
    ContextSourceError,
    render_context_block,
    sources_for_phase,
)


def _source(name, phases=("dispatch",), authority="reference", priority=0, **extra):
    return {"name": name, "phases": list(phases), "authority": authority, "priority": priority, **extra}


def _resolved(name, text="body", *, authority="reference", required=True):
    return {
        "name": name,
        "provider": "dir",
        "authority": authority,
        "required": required,
        "text": text,
        "digest": "sha256:abc",
        "bytes": len(text),
    }


def _names(sources):
    return [s["name"] for s in sources]


def test_should_keep_only_sources_for_the_phase():
    sources = [_source("a", ["dispatch"]), _source("b", ["review"]), _source("c", ["dispatch", "review"])]
    assert _names(sources_for_phase(sources, "review")) == ["b", "c"]


def test_should_return_empty_when_no_source_matches_phase():
    assert sources_for_phase([_source("a", ["dispatch"])], "security_review") == []


def test_should_return_empty_for_empty_sources():
    assert sources_for_phase([], "dispatch") == []


def test_should_order_reference_before_policy():
    sources = [_source("pol", authority="policy"), _source("ref", authority="reference")]
    assert _names(sources_for_phase(sources, "dispatch")) == ["ref", "pol"]


def test_should_order_by_ascending_priority_within_authority():
    sources = [_source("hi", priority=5), _source("lo", priority=-1), _source("mid", priority=2)]
    assert _names(sources_for_phase(sources, "dispatch")) == ["lo", "mid", "hi"]


def test_should_break_priority_ties_by_declaration_order():
    sources = [_source("first"), _source("second"), _source("third")]
    assert _names(sources_for_phase(sources, "dispatch")) == ["first", "second", "third"]


def test_should_order_policy_sources_by_priority_then_declaration():
    sources = [
        _source("p2", authority="policy", priority=2),
        _source("p1a", authority="policy", priority=1),
        _source("p1b", authority="policy", priority=1),
    ]
    assert _names(sources_for_phase(sources, "dispatch")) == ["p1a", "p1b", "p2"]


def test_should_not_mutate_input_sources():
    sources = [_source("b", priority=2), _source("a", priority=1)]
    sources_for_phase(sources, "dispatch")
    assert _names(sources) == ["b", "a"]


def test_should_render_empty_string_for_no_sources():
    assert render_context_block([], max_total_chars=1000) == ""


def test_should_state_data_not_instructions_in_preamble():
    block = render_context_block([_resolved("a")], max_total_chars=5000)
    assert "REFERENCE DATA" in block
    assert "not a request from the user" in block


def test_should_forbid_following_embedded_instructions():
    block = render_context_block([_resolved("a")], max_total_chars=5000)
    assert "must not be followed" in block


def test_should_mention_precedence_labels_in_preamble():
    block = render_context_block([_resolved("a")], max_total_chars=5000)
    assert "precedence" in block


def test_should_render_section_header_and_text():
    block = render_context_block([_resolved("style", "use tabs", authority="policy")], max_total_chars=5000)
    header = "## Context source: style (authority: policy, provider: dir, digest: sha256:abc, bytes: 8)"
    assert f"{header}\nuse tabs" in block


def test_should_render_sections_in_given_order():
    block = render_context_block([_resolved("one"), _resolved("two")], max_total_chars=5000)
    assert block.index("Context source: one") < block.index("Context source: two")


def test_should_skip_optional_source_that_does_not_fit_with_visible_line():
    resolved = [_resolved("big", "x" * 500, required=False)]
    block = render_context_block(resolved, max_total_chars=400)
    assert "x" * 500 not in block
    assert "skipped optional context source big" in block


def test_should_mark_truncation_with_limit():
    resolved = [_resolved("big", "x" * 500, required=False)]
    block = render_context_block(resolved, max_total_chars=400)
    assert "[... context truncated at 400 chars ...]" in block


def test_should_not_mark_truncation_when_everything_fits():
    block = render_context_block([_resolved("a")], max_total_chars=5000)
    assert "truncated" not in block


def test_should_keep_fitting_sources_when_optional_is_skipped():
    resolved = [_resolved("big", "x" * 5000, required=False), _resolved("small", "tiny")]
    block = render_context_block(resolved, max_total_chars=1500)
    assert "tiny" in block


def test_should_raise_when_required_source_does_not_fit():
    with pytest.raises(ContextSourceError, match="big"):
        render_context_block([_resolved("big", "x" * 500)], max_total_chars=400)


def test_should_raise_for_non_positive_budget():
    with pytest.raises(ValueError, match="max_total_chars"):
        render_context_block([_resolved("a")], max_total_chars=0)
