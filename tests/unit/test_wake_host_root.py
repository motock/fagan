"""Wake markers live under the host-local root (ADR 0012 decision 9).

Wake events are derived state the reconcile sweep recovers, so their marker
directory is a HostState concern: ``FileHostState.wake_dir()`` roots it at the
host root, resolved through the ``pipeline.server.PLAN_DIR`` binding.

Contract under test:

* ``request_wake`` writes the marker to ``<host root>/.wake/<plan>`` with the
  directory at mode 0o700.
* A traversal or otherwise invalid plan name is refused before any file or
  directory is created under the host root.
* ``claim_wakes`` consumes each marker exactly once, reading from the same root.
"""

import stat

import pytest

import pipeline.server  # noqa: F401 - pre-existing import cycle (see test_wake.py)
from pipeline import wake


@pytest.fixture()
def host_root(tmp_path, monkeypatch):
    root = tmp_path / "plans"
    root.mkdir()
    monkeypatch.setattr("pipeline.server.PLAN_DIR", root)
    return root


def test_request_wake_writes_the_marker_under_the_host_root(host_root):
    wake.request_wake("alpha", "approved")

    assert (host_root / ".wake" / "alpha").is_file()


def test_request_wake_creates_the_wake_dir_with_mode_0o700(host_root):
    wake.request_wake("alpha", "approved")

    assert stat.S_IMODE((host_root / ".wake").stat().st_mode) == 0o700


@pytest.mark.parametrize("plan_name", ["../escape", "a/b"])
def test_request_wake_refuses_an_invalid_plan_name_before_creating_anything(
    host_root, plan_name
):
    wake.request_wake(plan_name, "approved")

    assert list(host_root.iterdir()) == []


def test_claim_wakes_consumes_each_marker_once_from_the_host_root(host_root):
    wake.request_wake("alpha", "approved")

    assert wake.claim_wakes() == ["alpha"]
    assert wake.claim_wakes() == []
    assert not (host_root / ".wake" / "alpha").exists()
