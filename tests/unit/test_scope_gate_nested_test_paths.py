"""Story `files` scope gate: nested and Rust test layouts are test paths.

Cargo-workspace monorepos keep integration tests in per-crate ``tests/``
directories (``relay/tests/x.rs``, ``core/crypto/tests/x.rs``), not only in a
top-level ``tests/``.  Test paths are always allowed to change, so a story that
adds such a file must not trip the scope gate.
"""

import pytest

from pipeline import scope_gate
from pipeline.parsers import _is_test_file_path


@pytest.mark.parametrize(
    "path",
    [
        "tests/x.rs",
        "relay/tests/mailbox_queue.rs",
        "core/crypto/tests/x.rs",
        "crates/a/tests/fixtures/deep/x.rs",
        "web/tests/login.spec.ts",
        "relay/src/mailbox_test.rs",
    ],
)
def test_is_test_path_accepts_nested_and_rust_test_layouts(path):
    assert scope_gate.is_test_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "contests/x.rs",
        "relay/contests/x.rs",
        "relay/src/attests/x.rs",
        "relay/src/store.rs",
        "src/tests.rs",
        "relay/src/mailbox_tests.rs",
    ],
)
def test_is_test_path_rejects_lookalike_and_production_paths(path):
    assert scope_gate.is_test_path(path) is False


def test_scope_violations_allows_a_nested_rust_test_outside_declared_files():
    violations = scope_gate.scope_violations(
        ["relay/src/store.rs", "relay/tests/mailbox_queue.rs"],
        ["relay/src/store.rs"],
        {"relay"},
    )

    assert violations == []


def test_scope_violations_still_flags_an_undeclared_rust_production_file():
    violations = scope_gate.scope_violations(
        ["relay/src/store.rs", "relay/src/ws.rs", "relay/tests/mailbox_queue.rs"],
        ["relay/src/store.rs"],
        {"relay"},
    )

    assert violations == ["relay/src/ws.rs: outside this story's `files` scope"]


def test_remedy_directs_an_unresolvable_conflict_to_request_decision():
    assert "request_decision" in scope_gate.SCOPE_GATE_REMEDY


def test_remedy_no_longer_tells_the_agent_to_stop_and_report():
    assert "stop and report the conflict" not in scope_gate.SCOPE_GATE_REMEDY


def test_remedy_forbids_deleting_a_test_to_satisfy_the_gate():
    assert "never delete a test" in scope_gate.SCOPE_GATE_REMEDY.lower()


@pytest.mark.parametrize(
    "path",
    [
        "relay/tests/mailbox_queue.rs",
        "core/crypto/tests/x.rs",
        "relay/src/mailbox_test.rs",
        "pkg/test_a.py",
        "pkg/a_test.py",
    ],
)
def test_reviewer_untouched_finding_check_treats_these_as_test_files(path):
    assert _is_test_file_path(path) is True


@pytest.mark.parametrize(
    "path",
    ["relay/src/store.rs", "contests/x.rs", "pipeline/foo.py"],
)
def test_reviewer_untouched_finding_check_keeps_production_files_gated(path):
    assert _is_test_file_path(path) is False
