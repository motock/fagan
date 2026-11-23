"""A granted repository is cloned on its first read, and only then.

``read_root_kwargs`` names the managed clone a read serves. When the clone does
not exist yet and every check has passed (allow-list, containment, access hook),
the first read clones it once through ``remote_scope.clone_root_for``. An
existing clone is never re-cloned on a read; a refresh of it is covered in
test_repo_read_refresh.py. A failed clone answers
as a missing clone, is logged once without git detail, and is not retried for 60
seconds. Only the git clone (``repo_clone.ensure_clone``) and the monotonic clock
are mocked; the resolver and the allow-list are real.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import pytest

from pipeline import access_hooks, repo_clone, repo_read_access
from pipeline.access_hooks import ACCESS_HOOK_PLUGIN_ENV, AccessDecision

GRANTED_URL = "https://github.com/a/new"
OTHER_GRANTED_URL = "https://github.com/a/other"
UNGRANTED_URL = "https://github.com/b/two"
SECRET = "SECRET-STDERR-TOKEN"


class StubHook:
    """Stands in for the repository access hook; records every consultation."""

    def __init__(self, *, allowed=(GRANTED_URL, OTHER_GRANTED_URL)):
        self.allowed = allowed
        self.error: Exception | None = None
        self.calls: list[str] = []

    def check(self, namespace, repo, action, risk):
        self.calls.append(repo)
        if self.error is not None:
            raise self.error
        return AccessDecision(allowed=repo in self.allowed_repos(), reason="repo_not_granted")

    def allowed_repos(self) -> set[str]:
        return {url.removeprefix("https://") for url in self.allowed}


class FakeClock:
    """Stands in for ``time.monotonic``; the test moves ``now`` explicitly."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeClone:
    """Stands in for ``repo_clone.ensure_clone``: makes the directory or raises."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_urls: set[str] = set()
        self.error: BaseException = repo_clone.RepoCloneError(
            f"git clone failed: {SECRET}"
        )
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def __call__(self, url, environ=None, timeout=600):
        self.calls.append(url)
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        if url in self.fail_urls:
            raise self.error
        path = repo_clone.managed_path(url)
        path.mkdir(parents=True)
        (path / "README.md").write_text("hello new\n", encoding="utf-8")
        return path


@pytest.fixture(autouse=True)
def _no_plugin_env(monkeypatch):
    monkeypatch.delenv(ACCESS_HOOK_PLUGIN_ENV, raising=False)
    monkeypatch.delenv("PIPELINE_REPO_URL_HOSTS", raising=False)
    monkeypatch.delenv("PIPELINE_REPO_URL_ALLOW_FILE", raising=False)


@pytest.fixture(autouse=True)
def audit_records(monkeypatch):
    """Capture host audit appends so no test writes the real audit chain."""
    records: list[dict] = []
    monkeypatch.setattr(
        access_hooks, "append_host_audit", lambda **kwargs: records.append(kwargs)
    )
    return records


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(repo_read_access, "_clock", fake)
    repo_read_access._FAILED_AT.clear()
    return fake


@pytest.fixture
def repos_root(tmp_path: Path, monkeypatch) -> Path:
    """An empty managed-clone root: no repository is cloned yet."""
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setenv("PIPELINE_REPOS_ROOT", str(root))
    return root


@pytest.fixture
def hook(monkeypatch) -> StubHook:
    stub = StubHook()
    monkeypatch.setattr(access_hooks, "_hook", lambda: stub)
    return stub


@pytest.fixture
def fake_clone(monkeypatch) -> FakeClone:
    fake = FakeClone()
    monkeypatch.setattr(repo_clone, "ensure_clone", fake)
    return fake


def _root(repos_root: Path, owner: str, name: str) -> str:
    return os.path.normpath(os.path.realpath(repos_root / "github.com" / owner / name))


# --- the first read clones --------------------------------------------------------


def test_first_read_of_a_granted_repository_clones_it_once_and_names_the_root(
    repos_root, hook, fake_clone
):
    result = repo_read_access.read_root_kwargs(GRANTED_URL)

    assert result == {"root": _root(repos_root, "a", "new")}
    assert fake_clone.calls == [GRANTED_URL]
    assert os.path.isdir(result["root"])


def test_second_read_of_the_same_repository_does_not_clone_again(repos_root, hook, fake_clone):
    repo_read_access.read_root_kwargs(GRANTED_URL)
    repo_read_access.read_root_kwargs(GRANTED_URL)

    assert fake_clone.calls == [GRANTED_URL]


def test_existing_clone_is_not_cloned_again_on_a_read(repos_root, hook, fake_clone):
    clone = repos_root / "github.com" / "a" / "new"
    (clone / ".git").mkdir(parents=True)

    result = repo_read_access.read_root_kwargs(GRANTED_URL)

    assert result == {"root": _root(repos_root, "a", "new")}
    assert fake_clone.calls == []


def test_repo_url_none_returns_empty_and_clones_nothing(repos_root, hook, fake_clone):
    assert repo_read_access.read_root_kwargs(None) == {}
    assert fake_clone.calls == []


# --- refusals clone nothing -------------------------------------------------------


def test_access_hook_refusal_clones_nothing(repos_root, hook, fake_clone):
    hook.allowed = ()

    with pytest.raises(repo_read_access.RepoReadRefused):
        repo_read_access.read_root_kwargs(GRANTED_URL)

    assert fake_clone.calls == []
    assert not (repos_root / "github.com").exists()


def test_access_hook_that_raises_refuses_and_clones_nothing(repos_root, hook, fake_clone):
    hook.error = RuntimeError("plugin failure")

    with pytest.raises(repo_read_access.RepoReadRefused):
        repo_read_access.read_root_kwargs(GRANTED_URL)

    assert fake_clone.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/a/new",
        "http://github.com/a/new",
        "file:///tmp/a/new.git",
        "https://github.com/a/new?ref=main",
    ],
)
def test_url_off_the_allow_list_clones_nothing(repos_root, hook, fake_clone, url):
    with pytest.raises(repo_read_access.RepoUrlNotAllowed):
        repo_read_access.read_root_kwargs(url)

    assert fake_clone.calls == []


def test_refused_repository_clones_nothing_even_when_it_is_first_asked_twice(
    repos_root, hook, fake_clone
):
    hook.allowed = ()
    for _ in range(2):
        with pytest.raises(repo_read_access.RepoReadRefused):
            repo_read_access.read_root_kwargs(GRANTED_URL)

    assert fake_clone.calls == []


# --- a failed clone answers as a missing clone -----------------------------------


def test_clone_failure_answers_as_a_missing_clone_without_raising(repos_root, hook, fake_clone):
    fake_clone.fail_urls = {GRANTED_URL}

    result = repo_read_access.read_root_kwargs(GRANTED_URL)

    assert result == {"root": _root(repos_root, "a", "new")}
    assert not os.path.isdir(result["root"])


def test_clone_failure_logs_one_warning_without_git_detail(repos_root, hook, fake_clone, caplog):
    fake_clone.fail_urls = {GRANTED_URL}

    with caplog.at_level(logging.WARNING, logger="pipeline.repo_read_access"):
        repo_read_access.read_root_kwargs(GRANTED_URL)

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    record = warnings[0]
    assert record.event == "repo_first_read_clone_failed"
    assert record.repository == "github.com/a/new"
    assert SECRET not in caplog.text


def test_unexpected_clone_error_is_also_answered_as_a_missing_clone(
    repos_root, hook, fake_clone, caplog
):
    fake_clone.fail_urls = {GRANTED_URL}
    fake_clone.error = OSError(f"permission denied {SECRET}")

    with caplog.at_level(logging.WARNING, logger="pipeline.repo_read_access"):
        result = repo_read_access.read_root_kwargs(GRANTED_URL)

    assert not os.path.isdir(result["root"])
    assert SECRET not in caplog.text
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_a_read_within_sixty_seconds_of_a_failed_clone_does_not_retry(
    repos_root, hook, fake_clone, clock
):
    fake_clone.fail_urls = {GRANTED_URL}
    repo_read_access.read_root_kwargs(GRANTED_URL)
    clock.now += 59.0

    repo_read_access.read_root_kwargs(GRANTED_URL)

    assert fake_clone.calls == [GRANTED_URL]


@pytest.mark.parametrize(
    ("elapsed", "expected_attempts"),
    [(59.999, 1), (60.0, 2), (60.5, 2)],
    ids=["just-inside-window", "exactly-sixty", "after-window"],
)
def test_a_failed_clone_is_retried_only_once_sixty_seconds_have_passed(
    repos_root, hook, fake_clone, clock, elapsed, expected_attempts
):
    fake_clone.fail_urls = {GRANTED_URL}
    repo_read_access.read_root_kwargs(GRANTED_URL)
    clock.now += elapsed

    repo_read_access.read_root_kwargs(GRANTED_URL)

    assert len(fake_clone.calls) == expected_attempts


def test_a_retry_after_the_window_that_succeeds_serves_the_clone(repos_root, hook, fake_clone, clock):
    fake_clone.fail_urls = {GRANTED_URL}
    repo_read_access.read_root_kwargs(GRANTED_URL)
    fake_clone.fail_urls = set()
    clock.now += 60.0

    result = repo_read_access.read_root_kwargs(GRANTED_URL)

    assert os.path.isdir(result["root"])
    assert len(fake_clone.calls) == 2


def test_a_failure_for_one_repository_does_not_back_off_another(repos_root, hook, fake_clone):
    fake_clone.fail_urls = {GRANTED_URL}
    repo_read_access.read_root_kwargs(GRANTED_URL)

    result = repo_read_access.read_root_kwargs(OTHER_GRANTED_URL)

    assert fake_clone.calls == [GRANTED_URL, OTHER_GRANTED_URL]
    assert os.path.isdir(result["root"])


# --- concurrent first reads run one clone ----------------------------------------


def test_two_concurrent_first_reads_run_one_clone(repos_root, hook, fake_clone):
    fake_clone.gate = threading.Event()
    results: list[dict] = []
    errors: list[BaseException] = []

    def read() -> None:
        try:
            results.append(repo_read_access.read_root_kwargs(GRANTED_URL))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    first = threading.Thread(target=read)
    first.start()
    assert fake_clone.entered.wait(5)
    second = threading.Thread(target=read)
    second.start()
    fake_clone.gate.set()
    first.join(5)
    second.join(5)

    assert errors == []
    assert fake_clone.calls == [GRANTED_URL]
    assert results == [{"root": _root(repos_root, "a", "new")}] * 2
