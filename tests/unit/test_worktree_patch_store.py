"""Tests for the server-side patch RECORD STORE (WAP-6).

Target API added to ``pipeline/worktree_patch.py``::

    pipeline.worktree_patch.PATCH_TTL_SECONDS == 900
    pipeline.worktree_patch._TOKEN_SECRET   # process secret, minted once at import
    pipeline.worktree_patch.create_patch_record(
        plan_name, story_key, diff_text, paths, added_lines
    ) -> dict
    pipeline.worktree_patch.get_patch_record(patch_id) -> dict | None

The store is an in-process dict: the dashboard is a single process, so a
restart drops pending patches -- acceptable at a 15-minute TTL, and fail
closed (never persisted to disk in this story).

Scope note: this file grades ONLY what WAP-6 adds.  The apply half (the token
comparison and the ``status`` flip to ``"applied"``) belongs to WAP-7 and is
deliberately NOT graded here, so a later story can add it without breaking
these tests.  For the same reason ``__all__`` is checked by MEMBERSHIP, never
by exact contents.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import worktree_patch
from pipeline.worktree_patch import (
    PATCH_TTL_SECONDS,
    create_patch_record,
    get_patch_record,
)

# --------------------------------------------------------------------------
# store isolation
# --------------------------------------------------------------------------

#: Substrings that plausibly appear in the name of the module-level store dict.
_STORE_NAME_HINTS = ("store", "record", "patch", "pending", "cache")


def _store_attribute_names(module) -> list[str]:
    """Names of module-level dicts that plausibly hold the patch records.

    The brief does not pin the store's attribute name, so it is discovered
    rather than hard-coded: first by name hint, then by content (a dict whose
    values are records carrying ``patch_id``), and finally by minting a probe
    record and looking for the dict that now holds it.
    """
    dicts = [
        (name, value)
        for name, value in vars(module).items()
        if not name.startswith("__") and isinstance(value, dict)
    ]
    hinted = [name for name, _ in dicts if any(h in name.lower() for h in _STORE_NAME_HINTS)]
    if hinted:
        return hinted
    content = [
        name
        for name, value in dicts
        if value and all(isinstance(rec, dict) and "patch_id" in rec for rec in value.values())
    ]
    if content:
        return content
    probe = module.create_patch_record("probe-plan", "probe-story", "probe-diff", [], 0)
    probe_id = probe["patch_id"]
    return [
        name
        for name, value in vars(module).items()
        if isinstance(value, dict) and probe_id in value
    ]


class _StoreView:
    """Read-only view over the (possibly several) discovered store dicts."""

    def __init__(self, module, names: list[str]) -> None:
        self._module = module
        self._names = list(names)

    def _dicts(self) -> list[dict]:
        return [getattr(self._module, name) for name in self._names]

    def __len__(self) -> int:
        return sum(len(d) for d in self._dicts())

    def __contains__(self, patch_id: object) -> bool:
        return any(patch_id in d for d in self._dicts())

    def ids(self) -> list[str]:
        out: list[str] = []
        for d in self._dicts():
            out.extend(d.keys())
        return out

    def record(self, patch_id: str) -> dict | None:
        """The *stored* record object (not a copy), or ``None``."""
        for d in self._dicts():
            if patch_id in d:
                return d[patch_id]
        return None


@pytest.fixture(autouse=True)
def patch_store(monkeypatch) -> _StoreView:
    """Isolate the module-level store: swap it for a fresh, empty dict.

    Autouse so no test can leak a record into another test, and returned so
    tests can inspect the store directly (pruning, entry counts).
    """
    names = _store_attribute_names(worktree_patch)
    if not names:
        pytest.fail(
            "pipeline.worktree_patch exposes no module-level dict holding patch records"
        )
    for name in names:
        monkeypatch.setattr(worktree_patch, name, {})
    return _StoreView(worktree_patch, names)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

DIFF_A = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n"
DIFF_B = "--- a/docs/readme.md\n+++ b/docs/readme.md\n@@ -1,1 +1,1 @@\n-hi\n+hello\n"


def _parse_iso(value: object) -> datetime:
    assert isinstance(value, str), f"expected an ISO-8601 string, got {value!r}"
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(text)


def _make(diff: str = DIFF_A, paths: list[str] | None = None, added: int = 1) -> dict:
    return create_patch_record(
        "plan-alpha",
        "STORY-1",
        diff,
        ["src/app.py"] if paths is None else paths,
        added,
    )


def _set_expiry(patch_store: _StoreView, patch_id: str, delta_seconds: float) -> None:
    """Rewrite a stored record's ``expires_at`` relative to its ``created_at``.

    The offset is applied to the record's OWN ``created_at`` and re-serialized
    with the same convention, so the test does not pin naive-vs-aware UTC (the
    brief only requires an ISO-8601 UTC timestamp).
    """
    record = patch_store.record(patch_id)
    assert record is not None, f"record {patch_id!r} is not in the store"
    created = _parse_iso(record["created_at"])
    record["expires_at"] = (created + timedelta(seconds=delta_seconds)).isoformat()


def _expected_token(patch_id: str, diff_hash: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"{patch_id}:{diff_hash}".encode(),
        hashlib.sha256,
    ).hexdigest()


# --------------------------------------------------------------------------
# module constants / docstring contract
# --------------------------------------------------------------------------


def test_patch_ttl_seconds_is_900():
    assert PATCH_TTL_SECONDS == 900
    assert isinstance(PATCH_TTL_SECONDS, int)
    assert not isinstance(PATCH_TTL_SECONDS, bool)


def test_token_secret_is_a_process_secret_minted_at_import():
    secret = worktree_patch._TOKEN_SECRET
    assert isinstance(secret, str)
    assert len(secret) >= 32
    # Stable across repeated attribute access (minted once, not per call).
    assert worktree_patch._TOKEN_SECRET == secret


def test_token_secret_has_the_token_urlsafe_32_shape():
    """``secrets.token_urlsafe(32)`` -> 43 URL-safe base64 chars."""
    secret = worktree_patch._TOKEN_SECRET
    assert len(secret) == 43
    assert all(c.isalnum() or c in "-_" for c in secret)


def test_token_secret_is_not_regenerated_when_token_urlsafe_is_patched(monkeypatch):
    """The secret is bound at import; only ids are minted per call."""
    before = worktree_patch._TOKEN_SECRET
    monkeypatch.setattr(secrets, "token_urlsafe", lambda n: "sentinel")
    assert worktree_patch._TOKEN_SECRET == before
    result = _make()
    assert result["confirmation_token"] == _expected_token(
        result["patch_id"], result["diff_hash"], before
    )


def test_module_docstring_documents_the_single_process_store():
    doc = worktree_patch.__doc__ or ""
    normalized = doc.lower().replace("-", " ").replace("_", " ")
    assert "single process" in normalized, "docstring must state the single-process assumption"
    assert "in process" in normalized, "docstring must say the store is an in-process dict"
    assert "restart" in normalized, "docstring must say a restart drops pending patches"
    assert "persist" in normalized, "docstring must say patches are never persisted to disk"


def test_module_exports_the_new_names():
    exported = getattr(worktree_patch, "__all__", [])
    assert "create_patch_record" in exported
    assert "get_patch_record" in exported


# --------------------------------------------------------------------------
# create_patch_record -- happy path
# --------------------------------------------------------------------------


def test_create_patch_record_returns_ok_envelope():
    result = _make(paths=["src/app.py", "docs/readme.md"], added=7)
    assert result["ok"] is True
    for key in ("patch_id", "paths", "added_lines", "confirmation_token", "diff_hash"):
        assert key in result, f"missing key {key!r} in create_patch_record result"
    assert result["paths"] == ["src/app.py", "docs/readme.md"]
    assert result["added_lines"] == 7
    assert isinstance(result["patch_id"], str)
    assert isinstance(result["confirmation_token"], str)
    assert isinstance(result["diff_hash"], str)


def test_create_patch_record_mints_wp_prefixed_unique_ids():
    ids = {_make()["patch_id"] for _ in range(5)}
    assert len(ids) == 5, "patch ids must be unique"
    for patch_id in ids:
        assert patch_id.startswith("wp-")
        assert len(patch_id) > len("wp-")


def test_create_patch_record_stores_record_with_exact_fields(patch_store):
    result = _make(paths=["src/app.py"], added=3)
    record = get_patch_record(result["patch_id"])
    assert isinstance(record, dict)
    assert record["patch_id"] == result["patch_id"]
    assert record["plan_name"] == "plan-alpha"
    assert record["story_key"] == "STORY-1"
    assert record["diff_text"] == DIFF_A
    assert record["paths"] == ["src/app.py"]
    assert record["added_lines"] == 3
    assert record["status"] == "pending"
    assert record["diff_hash"] == result["diff_hash"]
    assert patch_store.record(result["patch_id"]) is not None


def test_stored_diff_hash_is_sha256_of_diff_text(patch_store):
    result = _make()
    expected = hashlib.sha256(DIFF_A.encode("utf-8")).hexdigest()
    assert result["diff_hash"] == expected
    assert patch_store.record(result["patch_id"])["diff_hash"] == expected


def test_create_patch_record_accepts_empty_paths_and_zero_added_lines(patch_store):
    result = create_patch_record("plan-alpha", "STORY-1", DIFF_A, [], 0)
    assert result["ok"] is True
    assert result["paths"] == []
    assert result["added_lines"] == 0
    record = get_patch_record(result["patch_id"])
    assert record["paths"] == []
    assert record["added_lines"] == 0


def test_create_patch_record_hashes_empty_diff_text(patch_store):
    result = create_patch_record("plan-alpha", "STORY-1", "", [], 0)
    assert result["diff_hash"] == hashlib.sha256(b"").hexdigest()
    assert get_patch_record(result["patch_id"])["diff_text"] == ""


def test_create_patch_record_does_not_mutate_the_paths_argument(patch_store):
    paths = ["src/app.py"]
    result = create_patch_record("plan-alpha", "STORY-1", DIFF_A, paths, 1)
    assert paths == ["src/app.py"]
    assert result["paths"] == ["src/app.py"]


# --------------------------------------------------------------------------
# create_patch_record -- timestamps
# --------------------------------------------------------------------------


def test_created_and_expires_timestamps_are_utc_and_ttl_apart(patch_store):
    result = _make()
    record = get_patch_record(result["patch_id"])
    created = _parse_iso(record["created_at"])
    expires = _parse_iso(record["expires_at"])
    assert (created.tzinfo is None) == (expires.tzinfo is None), (
        "created_at and expires_at must use the same (UTC) convention"
    )
    if created.tzinfo is not None:
        assert created.utcoffset() == timedelta(0)
        assert expires.utcoffset() == timedelta(0)
        now = datetime.now(timezone.utc)
    else:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((now - created).total_seconds()) < 120, "created_at must be 'now' in UTC"
    assert (expires - created).total_seconds() == pytest.approx(PATCH_TTL_SECONDS, abs=2)


# --------------------------------------------------------------------------
# get_patch_record -- unknown / malformed ids
# --------------------------------------------------------------------------


def test_get_patch_record_unknown_id_returns_none(patch_store):
    assert get_patch_record("wp-not-a-real-patch-id") is None
    assert len(patch_store) == 0


def test_get_patch_record_empty_and_none_ids_return_none(patch_store):
    assert get_patch_record("") is None
    assert get_patch_record(None) is None  # type: ignore[arg-type]


def test_get_patch_record_returns_the_stored_record(patch_store):
    result = _make()
    record = get_patch_record(result["patch_id"])
    assert record is not None
    assert record["diff_text"] == DIFF_A


def test_get_patch_record_does_not_flip_status_to_applied(patch_store):
    """The status flip belongs to the apply story (WAP-7), not to a read."""
    result = _make()
    get_patch_record(result["patch_id"])
    assert patch_store.record(result["patch_id"])["status"] == "pending"


# --------------------------------------------------------------------------
# TTL / expiry
# --------------------------------------------------------------------------


def test_expired_record_is_pruned_and_returns_none(patch_store):
    result = _make()
    patch_id = result["patch_id"]
    _set_expiry(patch_store, patch_id, -1)

    assert get_patch_record(patch_id) is None
    assert patch_id not in patch_store, "an expired record must be pruned from the store"
    assert get_patch_record(patch_id) is None
    assert len(patch_store) == 0


def test_expired_and_unknown_are_indistinguishable(patch_store):
    result = _make()
    patch_id = result["patch_id"]
    _set_expiry(patch_store, patch_id, -3600)
    assert get_patch_record(patch_id) is None
    assert get_patch_record("wp-unknown") is None


def test_record_expiring_right_now_is_not_returned(patch_store):
    result = _make()
    patch_id = result["patch_id"]
    _set_expiry(patch_store, patch_id, 0)
    assert get_patch_record(patch_id) is None


def test_record_with_future_expiry_is_returned(patch_store):
    result = _make()
    patch_id = result["patch_id"]
    _set_expiry(patch_store, patch_id, 60)
    record = get_patch_record(patch_id)
    assert record is not None
    assert record["diff_text"] == DIFF_A
    assert patch_id in patch_store, "a live record must not be pruned"


def test_freshly_created_record_is_not_expired(patch_store):
    result = _make()
    assert get_patch_record(result["patch_id"]) is not None
    assert result["patch_id"] in patch_store


# --------------------------------------------------------------------------
# confirmation token binding
# --------------------------------------------------------------------------


def test_confirmation_token_binds_patch_id_and_diff_hash():
    result = _make()
    expected = _expected_token(
        result["patch_id"], result["diff_hash"], worktree_patch._TOKEN_SECRET
    )
    assert result["confirmation_token"] == expected


def test_confirmation_token_is_a_hex_sha256_digest():
    token = _make()["confirmation_token"]
    assert len(token) == 64
    assert all(c in "0123456789abcdef" for c in token)


def test_two_records_get_different_tokens_for_the_same_diff():
    first = _make()
    second = _make()
    assert first["patch_id"] != second["patch_id"]
    assert first["diff_hash"] == second["diff_hash"]
    assert first["confirmation_token"] != second["confirmation_token"]
    assert second["confirmation_token"] == _expected_token(
        second["patch_id"], second["diff_hash"], worktree_patch._TOKEN_SECRET
    )


def test_token_is_not_the_bare_diff_hash():
    result = _make()
    assert result["confirmation_token"] != result["diff_hash"]


# --------------------------------------------------------------------------
# store bookkeeping / isolation
# --------------------------------------------------------------------------


def test_store_holds_one_entry_per_created_record(patch_store):
    first = _make(DIFF_A)
    second = _make(DIFF_B)
    assert len(patch_store) == 2
    assert set(patch_store.ids()) == {first["patch_id"], second["patch_id"]}


def test_two_records_do_not_cross_contaminate(patch_store):
    first = _make(DIFF_A, paths=["src/app.py"], added=1)
    second = _make(DIFF_B, paths=["docs/readme.md"], added=2)

    record_a = get_patch_record(first["patch_id"])
    record_b = get_patch_record(second["patch_id"])
    assert record_a["diff_text"] == DIFF_A
    assert record_b["diff_text"] == DIFF_B
    assert record_a["paths"] == ["src/app.py"]
    assert record_b["paths"] == ["docs/readme.md"]
    assert record_a["added_lines"] == 1
    assert record_b["added_lines"] == 2
    assert record_a["diff_hash"] == hashlib.sha256(DIFF_A.encode("utf-8")).hexdigest()
    assert record_b["diff_hash"] == hashlib.sha256(DIFF_B.encode("utf-8")).hexdigest()


def test_store_is_isolated_between_tests(patch_store):
    """Guards the autouse fixture itself: the store starts empty."""
    assert len(patch_store) == 0
