"""LDC-6: re-ingesting a plan with keyless stories must be rejected.

Without an explicit ``key`` a story is keyed by a fresh UUID (or a new
ticket id) on every ingest, so re-ingesting a plan whose manifest already
exists adds a *second* copy of that story next to the first, and both
dispatch. The fix is a pure helper, ``pipeline.ingest._keyless_reingest_error``,
called from ``_ingest_plan_impl`` right after the manifest path is resolved
and *before* the transaction and any ticket-provider side effect. Only
``overwrite=True`` (wholesale replace) is safe on a re-ingest.

These tests are RED until the implementation lands: the helper does not
exist yet, so the pure-helper tests fail with an AttributeError and the
integration tests fail because the re-ingest is not rejected.
"""

from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path

import pytest

# pipeline.server must load before pipeline.ingest: pipeline.ingest's module
# body imports pipeline.build_detect, which imports pipeline.server, which
# imports _ingest_plan_impl back from pipeline.ingest -- so importing
# pipeline.ingest first re-enters a partially-initialized pipeline.server.
import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_MD = REPO_ROOT / "REFERENCE.md"

# The exact error text the brief pins. Built from one template so the pure
# helper tests and the integration tests can never drift apart.
_ERROR_TEMPLATE = (
    "story {summary!r} has no explicit `key` and plan is already ingested: "
    "re-ingesting would mint a duplicate story that dispatches alongside the "
    "original. Give every story a `key`, or pass overwrite=True to replace "
    "the manifest."
)


def _expected_error(summary: str) -> str:
    return _ERROR_TEMPLATE.format(summary=summary)


def _keyless_reingest_error(*args, **kwargs):
    """Resolve the helper at call time so a missing helper is a clean RED."""
    return ingest_mod._keyless_reingest_error(*args, **kwargs)


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def keyless_plan_dir(tmp_path, monkeypatch):
    """Local copy of the shared ``plan_dir`` fixture (distinct name so ruff's
    F811 false positive, only ignored for the ``test_pipeline_mcp_server_*``
    file pattern, cannot fire)."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    # Pin the dispatch provider to "claude" so the preflight gate never fires
    # (it would otherwise consult the operator's real role registry).
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "claude")
    return d


class _RecordingProvider:
    """Ticket provider that records every Plane side effect and issues no ids,
    so manifest story keys come from the plan's own ``key`` (or a fresh UUID)."""

    enabled = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def create_epic(self, summary: str):
        self.calls.append(("create_epic", summary))

    def create_story(self, summary, description, epic_id, agent):
        self.calls.append(("create_story", summary))


@pytest.fixture
def provider(monkeypatch):
    prov = _RecordingProvider()
    monkeypatch.setattr(p, "get_ticket_provider", lambda: prov)
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: None)
    return prov


def _story(summary: str, **over) -> dict:
    base = {"summary": summary, "agent_instructions": "Do the thing."}
    base.update(over)
    return base


def _epic(summary: str, stories: list[dict]) -> dict:
    return {"summary": summary, "stories": stories}


def _write_plan(plan_dir: Path, name: str, epics: list[dict], repo_root: Path) -> dict:
    plan = {"name": name, "repo_root": str(repo_root), "epics": epics}
    (plan_dir / f"{name}.json").write_text(json.dumps(plan))
    return plan


def _ingest(plan_name: str, only_epics=None, overwrite: bool = False) -> dict:
    return ingest_mod._ingest_plan_impl(plan_name, only_epics, overwrite)


def _manifest(plan_dir: Path, name: str) -> dict:
    return json.loads((plan_dir / f"{name}.manifest.json").read_text())


# --------------------------------------------------------------------------- #
# 1. The helper exists, has the pinned signature, and is pure.
# --------------------------------------------------------------------------- #
def test_helper_exists_and_has_the_pinned_signature() -> None:
    helper = getattr(ingest_mod, "_keyless_reingest_error", None)
    assert helper is not None, "pipeline.ingest._keyless_reingest_error is missing"
    params = list(inspect.signature(helper).parameters)
    assert params == ["plan", "only_epics", "already_ingested", "overwrite"]


def test_helper_docstring_mentions_the_duplicate_and_overwrite() -> None:
    doc = inspect.getdoc(ingest_mod._keyless_reingest_error) or ""
    assert "duplicate" in doc.lower()
    assert "overwrite" in doc.lower()


def test_helper_is_defined_immediately_before_ingest_plan_impl() -> None:
    """The brief pins the helper's position: directly above _ingest_plan_impl."""
    src = inspect.getsource(ingest_mod)
    helper_at = src.index("def _keyless_reingest_error(")
    impl_at = src.index("def _ingest_plan_impl(")
    assert helper_at < impl_at
    between = src[helper_at:impl_at]
    # Nothing but the helper's own body and blank lines may sit between them.
    assert "def " not in between.split("def _keyless_reingest_error(", 1)[1], (
        "another top-level def sits between _keyless_reingest_error and "
        "_ingest_plan_impl"
    )


def test_helper_does_not_mutate_the_plan() -> None:
    plan = {"epics": [_epic("E1", [_story("A"), _story("B", key="S2")])]}
    snapshot = copy.deepcopy(plan)
    _keyless_reingest_error(plan, None, True, False)
    assert plan == snapshot


# --------------------------------------------------------------------------- #
# 2. Pure-helper contract.
# --------------------------------------------------------------------------- #
def test_overwrite_true_is_always_safe() -> None:
    plan = {"epics": [_epic("E1", [_story("A")])]}
    assert _keyless_reingest_error(plan, None, True, True) is None


def test_not_already_ingested_is_always_safe() -> None:
    plan = {"epics": [_epic("E1", [_story("A")])]}
    assert _keyless_reingest_error(plan, None, False, False) is None


def test_all_stories_keyed_is_safe() -> None:
    plan = {"epics": [_epic("E1", [_story("A", key="S1"), _story("B", key="S2")])]}
    assert _keyless_reingest_error(plan, None, True, False) is None


def test_empty_plan_is_safe() -> None:
    assert _keyless_reingest_error({"epics": []}, None, True, False) is None


def test_epic_with_no_stories_is_safe() -> None:
    plan = {"epics": [_epic("E1", [])]}
    assert _keyless_reingest_error(plan, None, True, False) is None


def test_one_keyless_story_names_its_summary() -> None:
    plan = {"epics": [_epic("E1", [_story("A", key="S1"), _story("B")])]}
    assert _keyless_reingest_error(plan, None, True, False) == _expected_error("B")


def test_first_keyless_story_in_plan_order_is_named() -> None:
    plan = {
        "epics": [
            _epic("E1", [_story("A", key="S1")]),
            _epic("E2", [_story("B"), _story("C")]),
        ]
    }
    assert _keyless_reingest_error(plan, None, True, False) == _expected_error("B")


def test_first_keyless_story_within_one_epic_is_named() -> None:
    plan = {"epics": [_epic("E1", [_story("A"), _story("B")])]}
    assert _keyless_reingest_error(plan, None, True, False) == _expected_error("A")


@pytest.mark.parametrize(
    "bad_key",
    [
        pytest.param(None, id="none"),
        pytest.param("", id="empty-string"),
        pytest.param(123, id="int"),
        pytest.param(["S1"], id="list"),
        pytest.param(False, id="bool"),
    ],
)
def test_key_that_is_not_a_non_empty_string_counts_as_keyless(bad_key) -> None:
    plan = {"epics": [_epic("E1", [_story("A", key=bad_key)])]}
    assert _keyless_reingest_error(plan, None, True, False) == _expected_error("A")


def test_whitespace_only_key_is_still_a_non_empty_string() -> None:
    """The contract is "non-empty string", not "non-blank string"."""
    plan = {"epics": [_epic("E1", [_story("A", key="  ")])]}
    assert _keyless_reingest_error(plan, None, True, False) is None


def test_keyless_story_in_an_excluded_epic_is_safe() -> None:
    plan = {
        "epics": [
            _epic("E1", [_story("A")]),
            _epic("E2", [_story("B", key="S2")]),
        ]
    }
    assert _keyless_reingest_error(plan, ["E2"], True, False) is None


def test_keyless_story_in_an_included_epic_is_rejected() -> None:
    plan = {
        "epics": [
            _epic("E1", [_story("A")]),
            _epic("E2", [_story("B", key="S2")]),
        ]
    }
    assert _keyless_reingest_error(plan, ["E1"], True, False) == _expected_error("A")


def test_empty_only_epics_filters_nothing() -> None:
    """``only_epics=[]`` is falsy, exactly as in the validation loop."""
    plan = {"epics": [_epic("E1", [_story("A")])]}
    assert _keyless_reingest_error(plan, [], True, False) == _expected_error("A")


def test_only_epics_naming_no_epic_is_safe() -> None:
    plan = {"epics": [_epic("E1", [_story("A")])]}
    assert _keyless_reingest_error(plan, ["NOPE"], True, False) is None


# --------------------------------------------------------------------------- #
# 3. Wiring: the check runs after the manifest path is resolved and before
#    the transaction / any ticket-provider side effect.
# --------------------------------------------------------------------------- #
def test_check_is_wired_after_manifest_path_and_before_the_transaction() -> None:
    src = inspect.getsource(ingest_mod._ingest_plan_impl)
    manifest_at = src.index("manifest_path = _store.manifest_path(plan_name)")
    call_at = src.index("_keyless_reingest_error(")
    txn_at = src.index("with _store.transaction(")
    assert manifest_at < call_at < txn_at
    assert "manifest_path.exists()" in src[call_at:txn_at]
    assert '{"ok": False, "error": keyless_error}' in src[call_at:txn_at]


# --------------------------------------------------------------------------- #
# 4. Integration through the real _ingest_plan_impl.
# --------------------------------------------------------------------------- #
def test_first_ingest_of_a_keyless_plan_succeeds(
    keyless_plan_dir, provider, tmp_path
) -> None:
    """The first ingest of a plan never needs keys."""
    _write_plan(
        keyless_plan_dir,
        "kp",
        [_epic("E1", [_story("A"), _story("B")])],
        tmp_path,
    )
    result = _ingest("kp")
    assert result["ok"] is True, result
    assert len(_manifest(keyless_plan_dir, "kp")["stories"]) == 2


def test_second_ingest_of_a_keyless_plan_is_rejected(
    keyless_plan_dir, provider, tmp_path
) -> None:
    _write_plan(keyless_plan_dir, "kp", [_epic("E1", [_story("A")])], tmp_path)
    assert _ingest("kp")["ok"] is True

    manifest_path = keyless_plan_dir / "kp.manifest.json"
    before_text = manifest_path.read_text()
    before_count = len(json.loads(before_text)["stories"])

    result = _ingest("kp")

    assert result == {"ok": False, "error": _expected_error("A")}
    assert manifest_path.read_text() == before_text, "manifest was rewritten"
    assert len(_manifest(keyless_plan_dir, "kp")["stories"]) == before_count


def test_rejected_reingest_makes_no_ticket_provider_side_effect(
    keyless_plan_dir, provider, tmp_path
) -> None:
    _write_plan(keyless_plan_dir, "kp", [_epic("E1", [_story("A")])], tmp_path)
    assert _ingest("kp")["ok"] is True
    calls_after_first = list(provider.calls)
    assert calls_after_first, "the first ingest should have created an epic/story"

    assert _ingest("kp")["ok"] is False

    assert provider.calls == calls_after_first


def test_second_ingest_with_overwrite_succeeds(
    keyless_plan_dir, provider, tmp_path
) -> None:
    _write_plan(keyless_plan_dir, "kp", [_epic("E1", [_story("A")])], tmp_path)
    assert _ingest("kp")["ok"] is True

    result = _ingest("kp", overwrite=True)

    assert result["ok"] is True, result
    assert len(_manifest(keyless_plan_dir, "kp")["stories"]) == 1


def test_fully_keyed_plan_reingests_fine(
    keyless_plan_dir, provider, tmp_path
) -> None:
    _write_plan(
        keyless_plan_dir,
        "kp",
        [_epic("E1", [_story("A", key="S1"), _story("B", key="S2")])],
        tmp_path,
    )
    assert _ingest("kp")["ok"] is True

    result = _ingest("kp")

    assert result["ok"] is True, result
    manifest = _manifest(keyless_plan_dir, "kp")
    assert set(manifest["stories"]) == {"S1", "S2"}


def test_reingest_with_only_epics_excluding_the_keyless_epic_succeeds(
    keyless_plan_dir, provider, tmp_path
) -> None:
    _write_plan(
        keyless_plan_dir,
        "kp",
        [
            _epic("E1", [_story("A")]),
            _epic("E2", [_story("B", key="S2")]),
        ],
        tmp_path,
    )
    assert _ingest("kp")["ok"] is True

    result = _ingest("kp", only_epics=["E2"])

    assert result["ok"] is True, result


def test_reingest_with_only_epics_including_the_keyless_epic_is_rejected(
    keyless_plan_dir, provider, tmp_path
) -> None:
    _write_plan(
        keyless_plan_dir,
        "kp",
        [
            _epic("E1", [_story("A")]),
            _epic("E2", [_story("B", key="S2")]),
        ],
        tmp_path,
    )
    assert _ingest("kp")["ok"] is True

    result = _ingest("kp", only_epics=["E1"])

    assert result == {"ok": False, "error": _expected_error("A")}


def test_keyless_reingest_error_is_reported_before_the_lock_is_taken(
    keyless_plan_dir, provider, tmp_path, monkeypatch
) -> None:
    """The rejection must not depend on acquiring the plan lock."""
    _write_plan(keyless_plan_dir, "kp", [_epic("E1", [_story("A")])], tmp_path)
    assert _ingest("kp")["ok"] is True

    class _NoTransactionStore:
        """Delegates manifest_path, refuses to open a transaction."""

        def __init__(self, inner):
            self._inner = inner

        def manifest_path(self, plan_name):
            return self._inner.manifest_path(plan_name)

        def transaction(self, plan_name):  # pragma: no cover - regression only
            raise AssertionError("the transaction must not be entered")

    monkeypatch.setattr(p, "_store", _NoTransactionStore(p._store))

    assert _ingest("kp") == {"ok": False, "error": _expected_error("A")}


# --------------------------------------------------------------------------- #
# 5. REFERENCE.md documents the new rule.
# --------------------------------------------------------------------------- #
def test_reference_documents_the_reingest_key_rule() -> None:
    text = REFERENCE_MD.read_text()
    heading = "## Re-ingest requires explicit story keys"
    anchor = "## Acceptance fixture grading"
    assert heading in text, "REFERENCE.md is missing the new section heading"
    assert anchor in text
    assert text.index(heading) < text.index(anchor), (
        "the new section must sit immediately before '## Acceptance fixture grading'"
    )
    section = text[text.index(heading) : text.index(anchor)]
    assert "overwrite=True" in section
    assert "`key`" in section
    assert "duplicate" in section
