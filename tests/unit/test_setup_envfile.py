"""Tests for pipeline.setup.envfile (guided-setup env-file read/render/write).

Contract under test (see the story brief):
- read_env_file(path) -> dict[str, str]: delegates to
  pipeline.env_file.parse_env_file; a missing file, a directory path or
  undecodable bytes yield {}; os.environ is never read or written.
- render_env_text(values) -> str: one ``KEY='value'`` line per entry in the
  mapping's insertion order, ending with exactly one newline; keys must match
  ^[A-Za-z_][A-Za-z0-9_]*$; values may not contain "\\n" or "\\r"; every value
  is single-quoted with an embedded quote encoded as the close/escape/reopen
  sequence ``'\\''``; nothing is printed or logged.
- write_env_file(path, values, *, secret_keys=(), now=None) -> dict: render
  first (a validation error touches nothing), idempotent when the bytes already
  match, timestamped ``.bak.<UTC YYYYMMDDTHHMMSSZ>`` backup of a differing
  existing file (never overwriting an existing backup), atomic temp-file +
  fsync + os.replace, mode 0600 when a secret key has a non-empty value else
  0644.
"""
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pipeline.env_file import parse_env_file
from pipeline.setup import envfile

# Values that must survive render -> parse unchanged. Deliberately excludes a
# value containing a single quote: parse_env_file strips exactly one quote
# pair and does not decode the shell escape sequence, so that case is graded
# by its exact rendered text instead (see the single-quote test below).
ROUND_TRIP_VALUES = {
    "PLAIN": "value",
    "WITH_SPACES": "two words  and  more",
    "HOME_REF": "$HOME/plans",
    "TILDE": "~/plans",
    "BACKTICKS": "`date`",
    "HASH": "before # after",
    "EQUALS": "a=b=c",
    "DOUBLE_QUOTE": 'say "hi"',
    "EMPTY": "",
    "PADDED": "  padded  ",
    "BACKSLASH": "C:\\path\\to",
    "UNICODE": "naïve — 日本語",
}

PINNED = datetime(2026, 10, 22, 12, 34, 56, tzinfo=UTC)


def _pinned_now():
    return PINNED


def _mode(path):
    return stat.S_IMODE(Path(path).stat().st_mode)


# ---------------------------------------------------------------------------
# render_env_text / read_env_file: round-trip and formatting
# ---------------------------------------------------------------------------

def test_render_then_parse_round_trips_every_value(tmp_path):
    text = envfile.render_env_text(ROUND_TRIP_VALUES)

    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    lines = text.splitlines()
    assert len(lines) == len(ROUND_TRIP_VALUES)
    for line in lines:
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*='.*'", line), line

    target = tmp_path / "env"
    target.write_text(text, encoding="utf-8")
    assert parse_env_file(target) == ROUND_TRIP_VALUES
    assert envfile.read_env_file(target) == ROUND_TRIP_VALUES


def test_render_preserves_insertion_order(tmp_path):
    text = envfile.render_env_text({"B": "2", "A": "1", "C": "3"})

    assert text == "B='2'\nA='1'\nC='3'\n"


def test_render_accepts_valid_key_shapes():
    assert envfile.render_env_text({"A": "1", "_x": "2", "A1_b": "3"}) == (
        "A='1'\n_x='2'\nA1_b='3'\n"
    )


def test_render_encodes_embedded_single_quote_as_close_escape_reopen():
    # value.replace("'", "'\\''") -> KEY='it'\''s'
    assert envfile.render_env_text({"QUOTED": "it's"}) == "QUOTED='it'\\''s'\n"


@pytest.mark.parametrize("bad_key", ["BAD-KEY", "1KEY", ""])
def test_render_rejects_invalid_key(bad_key):
    with pytest.raises(ValueError) as excinfo:
        envfile.render_env_text({bad_key: "v"})

    if bad_key:
        assert bad_key in str(excinfo.value)


@pytest.mark.parametrize("bad_value", ["line1\nline2", "line1\rline2"])
def test_render_rejects_newline_in_value_without_echoing_it(bad_value):
    with pytest.raises(ValueError) as excinfo:
        envfile.render_env_text({"SAFE_KEY": bad_value})

    message = str(excinfo.value)
    assert "SAFE_KEY" in message
    assert "line1" not in message


def test_read_env_file_delegates_to_parse_env_file(tmp_path):
    target = tmp_path / "env"
    target.write_bytes(b'# comment\nexport KEY="quoted value"\r\nNOEQUALS\nA=b=c\n')

    assert envfile.read_env_file(target) == parse_env_file(target)
    assert envfile.read_env_file(target) == {"KEY": "quoted value", "A": "b=c"}
    assert envfile.read_env_file(str(target)) == {"KEY": "quoted value", "A": "b=c"}


def test_read_env_file_returns_empty_dict_for_missing_directory_and_bad_bytes(tmp_path):
    assert envfile.read_env_file(tmp_path / "missing") == {}
    assert envfile.read_env_file(tmp_path) == {}

    undecodable = tmp_path / "undecodable"
    undecodable.write_bytes(b"KEY=\xff\xfe\n")
    assert envfile.read_env_file(undecodable) == {}


# ---------------------------------------------------------------------------
# write_env_file: creation, modes, backups, idempotence
# ---------------------------------------------------------------------------

def test_write_creates_missing_file_at_0644(tmp_path):
    target = tmp_path / "env"

    result = envfile.write_env_file(target, {"A": "1"})

    assert result == {
        "changed": True,
        "backup": None,
        "mode": "0644",
        "path": str(target),
    }
    assert _mode(target) == 0o644
    assert envfile.read_env_file(target) == {"A": "1"}
    # No temp file left behind next to the target.
    assert [p.name for p in tmp_path.iterdir()] == ["env"]


def test_write_uses_0600_when_secret_key_has_non_empty_value(tmp_path):
    target = tmp_path / "env"

    result = envfile.write_env_file(
        target, {"PLANE_API_KEY": "sekrit", "OTHER": "x"}, secret_keys=("PLANE_API_KEY",)
    )

    assert result["mode"] == "0600"
    assert _mode(target) == 0o600
    assert envfile.read_env_file(target)["PLANE_API_KEY"] == "sekrit"


@pytest.mark.parametrize("values", [{"PLANE_API_KEY": ""}, {"OTHER": "x"}])
def test_write_uses_0644_when_secret_key_empty_or_absent(tmp_path, values):
    target = tmp_path / "env"

    result = envfile.write_env_file(target, values, secret_keys=("PLANE_API_KEY",))

    assert result["mode"] == "0644"
    assert _mode(target) == 0o644


def test_write_backs_up_existing_file_byte_for_byte(tmp_path):
    target = tmp_path / "env"
    original = b"OLD=1\n# a comment\n"
    target.write_bytes(original)

    result = envfile.write_env_file(target, {"NEW": "2"}, now=_pinned_now)

    backup = tmp_path / "env.bak.20261022T123456Z"
    assert result["changed"] is True
    assert result["backup"] == str(backup)
    assert backup.read_bytes() == original
    assert envfile.read_env_file(target) == {"NEW": "2"}


def test_write_is_idempotent_and_creates_no_second_backup(tmp_path):
    target = tmp_path / "env"
    target.write_bytes(b"OLD=1\n")

    first = envfile.write_env_file(target, {"A": "1"}, now=_pinned_now)
    assert first["changed"] is True
    assert first["backup"] is not None
    mtime = target.stat().st_mtime_ns

    second = envfile.write_env_file(target, {"A": "1"}, now=_pinned_now)

    assert second == {
        "changed": False,
        "backup": None,
        "mode": "0644",
        "path": str(target),
    }
    assert target.stat().st_mtime_ns == mtime
    assert len(list(tmp_path.glob("*.bak.*"))) == 1


def test_write_validation_failure_leaves_file_untouched_and_no_backup(tmp_path):
    target = tmp_path / "env"
    target.write_bytes(b"KEEP=1\n")
    before = target.stat().st_mtime_ns

    for bad_values in ({"BAD-KEY": "v"}, {"OK": "a\nb"}):
        with pytest.raises(ValueError):
            envfile.write_env_file(target, bad_values, now=_pinned_now)

    assert target.read_bytes() == b"KEEP=1\n"
    assert target.stat().st_mtime_ns == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["env"]


def test_write_to_directory_path_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        envfile.write_env_file(tmp_path, {"A": "1"})

    assert tmp_path.is_dir()


def test_write_empty_mapping_writes_empty_file(tmp_path):
    target = tmp_path / "env"

    result = envfile.write_env_file(target, {})

    assert result["changed"] is True
    assert result["mode"] == "0644"
    assert target.read_bytes() == b""
    assert envfile.read_env_file(target) == {}


def test_write_rejects_positional_secret_keys(tmp_path):
    with pytest.raises(TypeError):
        envfile.write_env_file(tmp_path / "env", {}, ("PLANE_API_KEY",))


# ---------------------------------------------------------------------------
# write_env_file: backup naming boundaries and atomicity
# ---------------------------------------------------------------------------

def test_same_second_backups_get_distinct_names_and_first_survives(tmp_path):
    target = tmp_path / "env"
    target.write_bytes(b"GEN=0\n")

    first = envfile.write_env_file(target, {"GEN": "1"}, now=_pinned_now)
    second = envfile.write_env_file(target, {"GEN": "2"}, now=_pinned_now)

    assert first["backup"] == str(tmp_path / "env.bak.20261022T123456Z")
    assert second["backup"] == str(tmp_path / "env.bak.20261022T123456Z.1")
    assert Path(first["backup"]).read_bytes() == b"GEN=0\n"
    assert Path(second["backup"]).read_bytes() == b"GEN='1'\n"
    assert envfile.read_env_file(target) == {"GEN": "2"}


def test_existing_backup_is_never_overwritten(tmp_path):
    target = tmp_path / "env"
    target.write_bytes(b"GEN=0\n")
    squatter = tmp_path / "env.bak.20261022T123456Z"
    squatter.write_bytes(b"DO NOT TOUCH\n")

    result = envfile.write_env_file(target, {"GEN": "1"}, now=_pinned_now)

    assert result["backup"] == str(tmp_path / "env.bak.20261022T123456Z.1")
    assert squatter.read_bytes() == b"DO NOT TOUCH\n"


def test_default_now_produces_utc_timestamped_backup(tmp_path):
    target = tmp_path / "env"
    target.write_bytes(b"OLD=1\n")

    result = envfile.write_env_file(target, {"NEW": "2"})

    assert re.fullmatch(r".*\.bak\.\d{8}T\d{6}Z", result["backup"])
    stamp = datetime.strptime(
        result["backup"].rsplit(".bak.", 1)[1], "%Y%m%dT%H%M%SZ"
    ).replace(tzinfo=UTC)
    assert abs(datetime.now(UTC) - stamp) < timedelta(minutes=5)
    assert Path(result["backup"]).read_bytes() == b"OLD=1\n"


def test_write_fsyncs_and_replaces_from_a_temp_file_in_the_same_directory(
    tmp_path, monkeypatch
):
    target = tmp_path / "env"
    target.write_bytes(b"OLD=1\n")
    calls = {"fsync": 0, "replace": []}
    real_fsync = os.fsync
    real_replace = os.replace

    def counting_fsync(fd):
        calls["fsync"] += 1
        return real_fsync(fd)

    def counting_replace(src, dst, *args, **kwargs):
        calls["replace"].append((str(src), str(dst)))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", counting_fsync)
    monkeypatch.setattr(os, "replace", counting_replace)
    # Tolerate an implementation that binds the functions directly.
    if hasattr(envfile, "fsync"):
        monkeypatch.setattr(envfile, "fsync", counting_fsync)
    if hasattr(envfile, "replace"):
        monkeypatch.setattr(envfile, "replace", counting_replace)

    envfile.write_env_file(target, {"NEW": "2"}, now=_pinned_now)

    assert calls["fsync"] >= 1
    assert len(calls["replace"]) == 1
    src, dst = calls["replace"][0]
    assert dst == str(target)
    assert Path(src) != target
    assert Path(src).parent == target.parent
    assert envfile.read_env_file(target) == {"NEW": "2"}


def test_os_replace_failure_leaves_original_intact_and_no_temp_file(tmp_path, monkeypatch):
    target = tmp_path / "env"
    target.write_bytes(b"ORIGINAL=1\n")

    def boom(*args, **kwargs):
        raise OSError("replace exploded")

    monkeypatch.setattr(os, "replace", boom)
    if hasattr(envfile, "replace"):
        monkeypatch.setattr(envfile, "replace", boom)

    with pytest.raises(OSError):
        envfile.write_env_file(target, {"NEW": "2"}, now=_pinned_now)

    assert target.read_bytes() == b"ORIGINAL=1\n"
    # Only the original and the pre-replace backup may remain: no temp file.
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "env",
        "env.bak.20261022T123456Z",
    ]


# ---------------------------------------------------------------------------
# Purity: no os.environ access, no printing, no logging
# ---------------------------------------------------------------------------

class _RecordingEnv(dict):
    """dict that records every access so a test can prove os.environ is unused."""

    def __init__(self):
        super().__init__()
        self.touched = []

    def __getitem__(self, key):
        self.touched.append(("get", key))
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        self.touched.append(("set", key))
        super().__setitem__(key, value)

    def __delitem__(self, key):
        self.touched.append(("del", key))
        super().__delitem__(key)

    def __contains__(self, key):
        self.touched.append(("contains", key))
        return super().__contains__(key)

    def get(self, key, default=None):
        self.touched.append(("get", key))
        return super().get(key, default)

    def keys(self):
        self.touched.append(("keys", None))
        return super().keys()

    def items(self):
        self.touched.append(("items", None))
        return super().items()

    def values(self):
        self.touched.append(("values", None))
        return super().values()

    def __iter__(self):
        self.touched.append(("iter", None))
        return super().__iter__()


def test_module_never_reads_or_writes_os_environ(tmp_path, monkeypatch):
    target = tmp_path / "env"
    target.write_bytes(b"OLD=1\n")

    recording = _RecordingEnv()
    monkeypatch.setattr(os, "environ", recording)

    envfile.read_env_file(target)
    envfile.render_env_text({"A": "1"})
    envfile.write_env_file(target, {"A": "1"}, secret_keys=("A",), now=_pinned_now)

    assert recording.touched == []


def test_module_prints_and_logs_nothing(tmp_path, capsys):
    target = tmp_path / "env"
    target.write_bytes(b"OLD=1\n")

    envfile.read_env_file(target)
    envfile.render_env_text({"A": "1"})
    envfile.write_env_file(target, {"A": "1"}, secret_keys=("A",), now=_pinned_now)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert not hasattr(envfile, "logging")
