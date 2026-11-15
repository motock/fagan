"""Usage gate, cooldown and announcement state keyed by credential.

A credential key is a non-secret digest of a credential's purpose, name and
namespace. It is derived from identifiers only, never from the secret value.
The default credential (no key) keeps today's files, so state written before
this change reads unchanged, and one credential's pause or cooldown never
holds back a different credential.
"""

import pytest

from pipeline import server
from pipeline import usage as u
from pipeline import usage_cooldown as uc
from pipeline import usage_transitions as ut
from pipeline.parsers import _atomic_write_json

KEY_A = u.credential_key("model", "claude_oauth_token", "alpha")
KEY_B = u.credential_key("model", "claude_oauth_token", "beta")


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point every usage file at one temp directory (never ~/.claude)."""
    state = tmp_path / "usage_state.json"
    monkeypatch.setattr(u, "USAGE_STATE_PATH", state)
    monkeypatch.setattr(uc, "USAGE_STATE_PATH", str(state))
    return tmp_path


# ---------- credential key ----------


def test_credential_key_is_32_lowercase_hex_characters():
    key = u.credential_key("model", "claude_oauth_token", "alpha")

    assert len(key) == 32
    assert set(key) <= set("0123456789abcdef")


def test_credential_key_is_stable_for_the_same_identifiers():
    first = u.credential_key("model", "claude_oauth_token", "alpha")
    second = u.credential_key("model", "claude_oauth_token", "alpha")

    assert first == second


def test_credential_key_differs_when_only_the_namespace_differs():
    assert KEY_A != KEY_B


def test_credential_key_differs_when_only_the_purpose_differs():
    assert u.credential_key("model", "x", "alpha") != u.credential_key(
        "secret", "x", "alpha"
    )


def test_credential_key_never_contains_the_identifiers():
    key = u.credential_key("model", "claude_oauth_token", "alpha")

    for identifier in ("model", "claude_oauth_token", "alpha"):
        assert identifier not in key


def test_credential_key_encoding_cannot_collide_across_field_boundaries():
    left = u.credential_key("a\x00b", "c", "d")
    right = u.credential_key("a", "b\x00c", "d")

    assert left != right


@pytest.mark.parametrize(
    ("purpose", "name", "namespace"),
    [
        ("", "n", "ns"),
        ("p", "", "ns"),
        ("p", "n", ""),
        (None, "n", "ns"),
        ("p", 7, "ns"),
    ],
)
def test_credential_key_rejects_empty_or_non_string_fields(purpose, name, namespace):
    with pytest.raises(ValueError):
        u.credential_key(purpose, name, namespace)


@pytest.mark.parametrize(
    "bad_key", ["", "../escape", "ABCDEF" * 5 + "AB", "a" * 31, "g" * 32]
)
def test_keyed_paths_reject_a_malformed_key(state_dir, bad_key):
    with pytest.raises(ValueError):
        u.usage_state_path(bad_key)


# ---------- default credential keeps today's files ----------


def test_default_paths_are_todays_files(state_dir):
    assert u.usage_state_path() == state_dir / "usage_state.json"
    assert uc._cooldown_path() == state_dir / "usage_cooldown.json"
    assert ut._announced_path() == state_dir / "usage_announced.json"


def test_default_state_write_and_read_use_todays_file(state_dir):
    state = {"paused": True, "session_pct": 100, "week_pct": 3}

    u._write_usage_state(state)

    assert (state_dir / "usage_state.json").exists()
    assert u._read_usage_state() == state
    assert sorted(p.name for p in state_dir.iterdir()) == ["usage_state.json"]


def test_default_state_file_bytes_match_the_atomic_json_writer(state_dir):
    state = {"paused": False, "session_pct": 1}
    reference = state_dir / "reference.json"
    _atomic_write_json(reference, state)

    u._write_usage_state(state)

    assert (state_dir / "usage_state.json").read_bytes() == reference.read_bytes()


# ---------- keyed files sit beside the default ones and carry no identifiers ----------


def test_keyed_files_sit_beside_the_default_files_under_the_key(state_dir):
    assert u.usage_state_path(KEY_A) == state_dir / f"usage_state.{KEY_A}.json"
    assert uc._cooldown_path(KEY_A) == state_dir / f"usage_cooldown.{KEY_A}.json"
    assert ut._announced_path(KEY_A) == state_dir / f"usage_announced.{KEY_A}.json"


def test_keyed_file_names_never_contain_the_identifiers(state_dir):
    names = [
        u.usage_state_path(KEY_A).name,
        uc._cooldown_path(KEY_A).name,
        ut._announced_path(KEY_A).name,
    ]

    for name in names:
        assert "alpha" not in name
        assert "oauth" not in name


# ---------- one credential paused, the other dispatches ----------


def test_pausing_one_credential_leaves_the_other_unpaused(state_dir):
    u._write_usage_state({"paused": True, "session_pct": 100, "week_pct": 0}, KEY_A)

    assert u._read_usage_state(KEY_A)["paused"] is True
    assert u._read_usage_state(KEY_B) == {}
    assert u._read_usage_state() == {}


def test_a_keyed_write_never_touches_the_default_state_file(state_dir):
    u._write_usage_state({"paused": True}, KEY_A)

    assert not (state_dir / "usage_state.json").exists()


def test_cooldown_latched_for_one_credential_leaves_the_others_open(state_dir):
    uc.trip(credential=KEY_A)

    assert uc.active(KEY_A) is True
    assert uc.active(KEY_B) is False
    assert uc.active() is False


def test_announcement_state_is_per_credential(state_dir):
    _atomic_write_json(ut._announced_path(KEY_A), {"paused": True})

    assert ut._read_announced(KEY_A) is True
    assert ut._read_announced(KEY_B) is False
    assert ut._read_announced() is False


def test_announce_records_the_transition_for_its_credential_only(
    state_dir, monkeypatch
):
    notified = []
    monkeypatch.setattr(server, "_notify_user", lambda *a, **k: notified.append(a))

    event = ut.announce(True, {}, credential=KEY_A)

    assert event == "usage_paused"
    assert len(notified) == 1
    assert ut._read_announced(KEY_A) is True
    assert ut._read_announced(KEY_B) is False


# ---------- fail closed on a malformed credential key ----------


def test_cooldown_active_rejects_a_malformed_key_instead_of_failing_open(state_dir):
    with pytest.raises(ValueError):
        uc.active("../escape")


def test_cooldown_trip_rejects_a_malformed_key(state_dir):
    with pytest.raises(ValueError):
        uc.trip(credential="../escape")


def test_announce_rejects_a_malformed_key(state_dir, monkeypatch):
    monkeypatch.setattr(server, "_notify_user", lambda *a, **k: None)

    with pytest.raises(ValueError):
        ut.announce(True, {}, credential="../escape")
