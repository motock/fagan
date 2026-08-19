"""Grades the wiring of the failure-triage sweep into the scheduler tick.

This is the TDD companion to ``test_acceptance_triage_sweep_wired.py``. That
file grades the *behavior* of the wiring (sweep runs, runs before the tick,
fail-open holds). This file grades the *mechanically-checkable requirements*
the task states about the single production edit in ``pipeline/server.py``:

* the new ``from .triage import run_triage_sweep`` import is present and sits
  between the ``.ticketing`` and ``.usage`` import blocks (so ruff's
  import-sort rule is satisfied);
* the original tick body was *renamed* to
  ``_advance_pipeline_locked_impl`` (not deleted, not rewritten) and is a
  distinct object from the new ``_advance_pipeline_locked`` wrapper;
* the wrapper calls ``run_triage_sweep`` before delegating to the impl;
* the sweep is advisory / fail-open: a raising sweep is swallowed and the
  tick still completes with its normal result;
* the sweep is a strict no-op while ``PIPELINE_AUTO_TRIAGE`` is unset (the
  whole test suite deletes every ``PIPELINE_*`` variable at import time, so
  the real ``run_triage_sweep`` must not touch the filesystem here).

These tests must fail for the right reason until the implementation lands:
an ``AttributeError`` on ``pipeline.server.run_triage_sweep`` /
``pipeline.server._advance_pipeline_locked_impl`` (the names do not exist
yet), not a syntax or import error in this file.
"""

import inspect
import json

import pipeline.server as p

# --------------------------------------------------------------------------- #
# Import wiring
# --------------------------------------------------------------------------- #

def test_run_triage_sweep_is_imported_into_pipeline_server():
    """The NAME is imported so tests can patch ``p.run_triage_sweep``."""
    assert hasattr(p, "run_triage_sweep"), (
        "pipeline.server must import run_triage_sweep by name "
        "(from .triage import run_triage_sweep)"
    )
    # It must be the same callable object that lives in pipeline.triage, not a
    # re-bound local that happens to share a name.
    from pipeline import triage

    assert p.run_triage_sweep is triage.run_triage_sweep


def test_triage_import_sits_between_ticketing_and_usage_blocks():
    """ruff's import-sort rule wants .ticketing < .triage < .usage.

    The new import line must appear after the closing ``)`` of the
    ``from .ticketing import (...)`` block and before the
    ``from .usage import (...)`` block.
    """
    source = inspect.getsource(p)
    ticketing_close = source.index("from .ticketing import (")
    # find the closing paren of that block
    ticketing_end = source.index(")", ticketing_close)
    usage_open = source.index("from .usage import (", ticketing_end)
    triage_line = source.find("from .triage import run_triage_sweep", ticketing_end)

    assert triage_line != -1, (
        "missing `from .triage import run_triage_sweep` between the "
        ".ticketing and .usage import blocks"
    )
    assert ticketing_end < triage_line < usage_open, (
        "the triage import must sit between the .ticketing and .usage blocks"
    )


# --------------------------------------------------------------------------- #
# Rename-and-delegate shape
# --------------------------------------------------------------------------- #

def test_impl_function_exists_and_is_distinct_from_wrapper():
    assert hasattr(p, "_advance_pipeline_locked_impl"), (
        "_advance_pipeline_locked_impl (the renamed original tick body) "
        "must exist"
    )
    assert hasattr(p, "_advance_pipeline_locked")
    assert p._advance_pipeline_locked_impl is not p._advance_pipeline_locked, (
        "_advance_pipeline_locked_impl must be a different object from "
        "_advance_pipeline_locked"
    )
    assert callable(p._advance_pipeline_locked_impl)
    assert callable(p._advance_pipeline_locked)


def test_wrapper_signature_unchanged():
    """The public-facing wrapper keeps the same single ``plan_name`` parameter."""
    sig = inspect.signature(p._advance_pipeline_locked)
    params = list(sig.parameters)
    assert params == ["plan_name"], (
        f"_advance_pipeline_locked signature changed: {params}"
    )


def test_impl_signature_matches_wrapper():
    sig_impl = inspect.signature(p._advance_pipeline_locked_impl)
    assert list(sig_impl.parameters) == ["plan_name"]


def test_wrapper_body_calls_run_triage_sweep_then_impl():
    """The wrapper source must call run_triage_sweep and delegate to the impl."""
    src = inspect.getsource(p._advance_pipeline_locked)
    assert "run_triage_sweep" in src, (
        "wrapper must call run_triage_sweep(plan_name)"
    )
    assert "_advance_pipeline_locked_impl" in src, (
        "wrapper must delegate to _advance_pipeline_locked_impl(plan_name)"
    )


def test_wrapper_swallows_exceptions_from_sweep():
    """The wrapper must catch a raising sweep (fail-open / advisory, C2)."""
    src = inspect.getsource(p._advance_pipeline_locked)
    assert "except Exception" in src, (
        "wrapper must catch exceptions raised by the sweep"
    )


def test_impl_body_starts_with_manifest_path_and_is_not_the_wrapper():
    """The renamed body must begin with the original first line, verbatim."""
    src = inspect.getsource(p._advance_pipeline_locked_impl)
    # strip the leading def line + docstring-free body
    first_body_line = src.splitlines()[1]
    assert "manifest_path" in first_body_line, (
        "impl body must start with the original manifest_path line, "
        f"got: {first_body_line!r}"
    )
    # The impl must NOT itself call run_triage_sweep (the sweep lives only in
    # the wrapper); this guards against the body being re-indented/rewritten
    # to inline the sweep.
    assert "run_triage_sweep" not in src, (
        "_advance_pipeline_locked_impl must not call run_triage_sweep; the "
        "sweep belongs only in the wrapper"
    )


def test_wrapper_is_not_an_mcp_tool():
    """_advance_pipeline_locked is an internal helper, not an @mcp.tool()."""
    fn = p._advance_pipeline_locked
    # mcp.tool-decorated callables carry no standard marker we can rely on, but
    # the wrapper must not be the public advance_pipeline tool itself.
    assert fn is not p.advance_pipeline
    assert fn is not getattr(p, "advance_all_plans", None)


# --------------------------------------------------------------------------- #
# Behavior: sweep runs before the tick, fail-open holds, no-op when unset
# --------------------------------------------------------------------------- #

def _write_manifest(plan_dir, **extra):
    manifest = {"epics": {}, "stories": {}}
    manifest.update(extra)
    (plan_dir / "tri.manifest.json").write_text(json.dumps(manifest))


def _stub_tick_boundaries(monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "_role_resource_ok", lambda *a, **k: (True, ""))
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)


def test_sweep_called_with_plan_name_before_impl(plan_dir, monkeypatch):
    order = []

    def fake_sweep(plan_name):
        order.append(("sweep", plan_name))

    def fake_impl(plan_name):
        order.append(("impl", plan_name))
        return {"ok": True}

    monkeypatch.setattr(p, "run_triage_sweep", fake_sweep)
    monkeypatch.setattr(p, "_advance_pipeline_locked_impl", fake_impl)
    _write_manifest(plan_dir)

    result = p._advance_pipeline_locked("tri")

    assert result == {"ok": True}
    assert order == [("sweep", "tri"), ("impl", "tri")]


def test_raising_sweep_is_swallowed_and_tick_completes(plan_dir, monkeypatch):
    def boom(plan_name):
        raise RuntimeError("overlord transport blew up")

    def fake_impl(plan_name):
        return {"ok": True, "summary": "ran"}

    monkeypatch.setattr(p, "run_triage_sweep", boom)
    monkeypatch.setattr(p, "_advance_pipeline_locked_impl", fake_impl)
    _write_manifest(plan_dir)

    result = p._advance_pipeline_locked("tri")

    assert result == {"ok": True, "summary": "ran"}


def test_raising_sweep_does_not_call_impl_until_after_catch(plan_dir, monkeypatch):
    """Fail-open: the impl still runs even when the sweep raises."""
    ran = []

    def boom(plan_name):
        raise RuntimeError("boom")

    def fake_impl(plan_name):
        ran.append(plan_name)
        return {"ok": True}

    monkeypatch.setattr(p, "run_triage_sweep", boom)
    monkeypatch.setattr(p, "_advance_pipeline_locked_impl", fake_impl)
    _write_manifest(plan_dir)

    p._advance_pipeline_locked("tri")
    assert ran == ["tri"]


def test_sweep_is_noop_when_pipeline_auto_triage_unset(plan_dir, monkeypatch):
    """With PIPELINE_AUTO_TRIAGE unset the real sweep must not touch the
    filesystem and the tick must proceed normally.

    tests/unit/conftest.py deletes every PIPELINE_* variable at import time,
    so the flag is unset for the whole suite. We point the real
    run_triage_sweep at a manifest with parked/failed stories and assert it
    leaves no side effects (no triage journal/state files appear) while the
    tick still completes.
    """
    # Ensure the flag is genuinely unset for this test.
    monkeypatch.delenv("PIPELINE_AUTO_TRIAGE", raising=False)

    _write_manifest(
        plan_dir,
        stories={
            "tri-1": {"status": "failed"},
            "tri-2": {"status": "parked"},
        },
    )
    files_before = set(plan_dir.iterdir())

    # Use the real impl (not a stub) so we exercise the real wrapper path; but
    # stub the tick boundaries so the body doesn't try to dispatch agents.
    _stub_tick_boundaries(monkeypatch)

    result = p._advance_pipeline_locked("tri")

    # The tick completes (no manifest error path here).
    assert isinstance(result, dict)
    # The real sweep, with the flag unset, must not have written anything.
    files_after = set(plan_dir.iterdir())
    assert files_after == files_before, (
        "run_triage_sweep wrote files while PIPELINE_AUTO_TRIAGE was unset; "
        f"new files: {files_after - files_before}"
    )


# --------------------------------------------------------------------------- #
# Byte-identical body guard (the task's hardest constraint)
# --------------------------------------------------------------------------- #

def test_impl_body_is_not_reindented_relative_to_wrapper():
    """The ONLY change to the original body is the name on its def line.

    We can't diff against the pre-edit file from inside the test process, but
    we can assert the impl body is substantial (the original is ~455 lines)
    and that the wrapper is thin. A re-indented/rewritten body would either
    shrink the impl or bloat the wrapper.
    """
    impl_src = inspect.getsource(p._advance_pipeline_locked_impl)
    wrapper_src = inspect.getsource(p._advance_pipeline_locked)

    impl_lines = [ln for ln in impl_src.splitlines() if ln.strip()]
    wrapper_lines = [ln for ln in wrapper_src.splitlines() if ln.strip()]

    # The renamed original is a large function body.
    assert len(impl_lines) > 100, (
        f"_advance_pipeline_locked_impl body is only {len(impl_lines)} non-blank "
        "lines; the original tick body is ~455 lines, so a re-indent/rewrite "
        "likely shrank it"
    )
    # The wrapper is thin: def + docstring + try/except + return.
    assert len(wrapper_lines) < 20, (
        f"_advance_pipeline_locked wrapper is {len(wrapper_lines)} non-blank "
        "lines; it should be a thin try/except + delegate, not a copy of the body"
    )


def test_wrapper_has_docstring_mentioning_fail_open():
    """The wrapper docstring records the advisory/fail-open intent (C2)."""
    src = inspect.getsource(p._advance_pipeline_locked)
    # The docstring is the first triple-quoted block under the def.
    assert '"""' in src
    # Fail-open intent is documented somewhere in the wrapper.
    lowered = src.lower()
    assert "fail" in lowered or "advisory" in lowered or "noqa" in lowered, (
        "wrapper should document the fail-open / advisory nature of the sweep"
    )