"""``HostState.job_state_root`` resolves where detached job state lives.

The rule is shared by the review, merge re-verify and grading channels, so it
is graded once here through the public seam:

* ``PIPELINE_STATE_DIR`` unset or empty -> the plans directory,
* an absolute override -> that path, as given,
* a relative override -> joined under the plans directory, never the CWD,
* the environment is read on every call, not cached at construction,
* resolving the root creates nothing on disk,
* a trailing separator or ``..`` is joined as given: containment is the
  caller's job, so the exact join is pinned rather than normalised away.
"""

import pytest

import pipeline.server as server_mod

try:  # the seam under test may not exist yet -> RED, not a collection error
    from pipeline import host_state
except ImportError:  # pragma: no cover - exercised only while the seam is missing
    host_state = None

STATE_DIR_ENV = "PIPELINE_STATE_DIR"


def _hs():
    """Build the host state through the only public constructor."""
    assert host_state is not None, "pipeline/host_state.py does not exist yet"
    return host_state.build_host_state()


@pytest.fixture
def plans_dir(tmp_path, monkeypatch):
    """A plans directory the host state resolves its root through."""
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr(server_mod, "PLAN_DIR", plans)
    monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    return plans


def test_unset_override_resolves_to_the_plans_directory(plans_dir):
    assert _hs().job_state_root() == plans_dir


def test_empty_override_resolves_to_the_plans_directory(plans_dir, monkeypatch):
    monkeypatch.setenv(STATE_DIR_ENV, "")

    assert _hs().job_state_root() == plans_dir


def test_absolute_override_resolves_to_that_path(plans_dir, tmp_path, monkeypatch):
    state = tmp_path / "elsewhere" / "state"
    monkeypatch.setenv(STATE_DIR_ENV, str(state))

    assert _hs().job_state_root() == state


def test_relative_override_resolves_under_the_plans_directory(
    plans_dir, tmp_path, monkeypatch
):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv(STATE_DIR_ENV, "state")

    root = _hs().job_state_root()

    assert root == plans_dir / "state"


def test_relative_override_is_never_resolved_against_the_cwd(
    plans_dir, tmp_path, monkeypatch
):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv(STATE_DIR_ENV, "state")

    root = _hs().job_state_root()

    assert not root.is_relative_to(cwd)


def test_override_is_read_on_every_call(plans_dir, tmp_path, monkeypatch):
    hs = _hs()
    state = tmp_path / "absolute-state"
    monkeypatch.setenv(STATE_DIR_ENV, "relative-state")
    first = hs.job_state_root()

    monkeypatch.setenv(STATE_DIR_ENV, str(state))
    second = hs.job_state_root()

    monkeypatch.delenv(STATE_DIR_ENV)
    third = hs.job_state_root()
    assert (first, second, third) == (plans_dir / "relative-state", state, plans_dir)


def test_job_state_root_creates_nothing_on_disk(plans_dir, tmp_path, monkeypatch):
    state = tmp_path / "not" / "yet" / "created"
    monkeypatch.setenv(STATE_DIR_ENV, str(state))

    _hs().job_state_root()
    monkeypatch.setenv(STATE_DIR_ENV, "relative-not-yet")
    _hs().job_state_root()

    assert not state.exists()
    assert not (plans_dir / "relative-not-yet").exists()
    assert list(plans_dir.iterdir()) == []


def test_parent_traversal_is_joined_as_given_not_normalised(plans_dir, monkeypatch):
    monkeypatch.setenv(STATE_DIR_ENV, "../escape")

    root = _hs().job_state_root()

    assert str(root) == f"{plans_dir}/../escape"


def test_trailing_separator_on_relative_override_is_joined_as_given(
    plans_dir, monkeypatch
):
    monkeypatch.setenv(STATE_DIR_ENV, "state/")

    root = _hs().job_state_root()

    assert root == plans_dir / "state"
