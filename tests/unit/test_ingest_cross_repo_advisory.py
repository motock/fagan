"""Ingest-time advisory: a brief names another connected repository, but no blocked_by."""

import json
import logging
from pathlib import Path

import pytest

import pipeline.server as p
from pipeline import ingest as ingest_mod
from pipeline import persistence as ppers
from pipeline import repo_listing_hooks
from pipeline.ingest_advisories import cross_repo_warning, story_advisories
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _clear_caches,
    _isolate_usage_state,
)

REPOS = ["github.com/acme/api", "github.com/acme/web", "github.com/acme/ui"]
PLAN_REPO = "github.com/acme/web"
PLAN_REPO_URL = "https://github.com/acme/web.git"
PREFLIGHT = "Preflight: test fixture - not a real plan\n"


def _story(text, **extra):
    return {"agent_instructions": text, **extra}


def _listing_answer(repos):
    return {
        "ok": True,
        "repositories": [{"repo": r, "level": None, "state": None} for r in repos],
    }


@pytest.fixture
def listing(monkeypatch):
    def install(answer):
        monkeypatch.setattr(
            repo_listing_hooks, "list_repositories_result", lambda store: answer
        )

    install(_listing_answer(REPOS))
    return install


def _write(root, rel, lines):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n" * lines)


def test_should_warn_when_full_repository_is_named_without_blocked_by():
    text = cross_repo_warning(_story("Call github.com/acme/api first."), PLAN_REPO, REPOS)

    assert text is not None
    assert text.startswith("the brief names another connected repository (")
    assert "github.com/acme/api" in text
    assert text.endswith(
        "but the story has no blocked_by; if it depends on a change there, "
        "order it with blocked_by"
    )


def test_should_warn_when_only_owner_and_name_are_named():
    text = cross_repo_warning(_story("Call acme/api for the token."), PLAN_REPO, REPOS)

    assert text is not None
    assert "github.com/acme/api" in text


def test_should_not_warn_when_blocked_by_is_set():
    story = _story("Call acme/api.", blocked_by=["api-plan:s1"])

    assert cross_repo_warning(story, PLAN_REPO, REPOS) is None


def test_should_warn_when_blocked_by_is_an_empty_list():
    story = _story("Call acme/api first.", blocked_by=[])

    assert cross_repo_warning(story, PLAN_REPO, REPOS) is not None


def test_should_warn_when_blocked_by_is_none():
    story = _story("Call acme/api first.", blocked_by=None)

    assert cross_repo_warning(story, PLAN_REPO, REPOS) is not None


def test_should_not_warn_when_only_the_plan_repository_is_named():
    story = _story("Edit acme/web/pipeline.py only.")

    assert cross_repo_warning(story, PLAN_REPO, REPOS) is None


@pytest.mark.parametrize(
    "text", ["acme/apiary", "xacme/api", "acme/api.py", "acme/api-v2", "gitlab.com/acme/api"]
)
def test_should_not_match_a_longer_or_embedded_name(text):
    story = _story(f"See {text} for details.")

    assert cross_repo_warning(story, PLAN_REPO, ["github.com/acme/api"]) is None


def test_should_match_owner_and_name_case_insensitively():
    text = cross_repo_warning(_story("Call ACME/API first."), PLAN_REPO, REPOS)

    assert text is not None
    assert "github.com/acme/api" in text


def test_should_not_match_a_short_name_by_owner_and_name_alone():
    story = _story("Call acme/ui.")

    assert cross_repo_warning(story, PLAN_REPO, REPOS) is None


def test_should_match_a_short_name_by_its_full_form():
    text = cross_repo_warning(_story("Call github.com/acme/ui first."), PLAN_REPO, REPOS)

    assert text is not None
    assert "github.com/acme/ui" in text


def test_should_list_five_sorted_repositories_when_six_are_named():
    others = [f"github.com/acme/svc{i}" for i in range(6)]
    repos = list(reversed(others)) + [PLAN_REPO]
    story = _story("Touches " + ", ".join(others) + " first.")

    text = cross_repo_warning(story, PLAN_REPO, repos)

    expected = ", ".join(others[:5])
    assert f"({expected})" in text
    assert "svc5" not in text


@pytest.mark.parametrize("brief", ["", None, 42])
def test_should_return_none_for_a_brief_that_is_not_usable(brief):
    story = {} if brief is None else {"agent_instructions": brief}

    assert cross_repo_warning(story, PLAN_REPO, REPOS) is None


def test_should_return_none_when_agent_instructions_is_missing():
    assert cross_repo_warning({"summary": "s"}, PLAN_REPO, REPOS) is None


def test_should_warn_for_every_named_repository_when_plan_repository_is_none():
    story = _story("Call acme/web and acme/api first.")

    text = cross_repo_warning(story, None, REPOS)

    assert "github.com/acme/web" in text
    assert "github.com/acme/api" in text


def test_should_return_no_cross_repo_advisory_when_listing_is_not_ok(listing, tmp_path):
    listing({"ok": False, "reason": "repositories_unavailable"})
    _write(tmp_path, "pipeline/near.py", 999)
    story = _story("Call acme/api first.", files=["pipeline/near.py"])

    advisories = story_advisories(story, str(tmp_path), PLAN_REPO_URL, object())

    assert [event for event, _ in advisories] == ["scope_headroom"]


def test_should_log_one_warning_with_error_type_only_when_listing_raises(
    monkeypatch, caplog, tmp_path
):
    def boom(store):
        raise RuntimeError("token abc123 leaked")

    monkeypatch.setattr(repo_listing_hooks, "list_repositories_result", boom)
    story = _story("Call acme/api first.")

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        advisories = story_advisories(story, str(tmp_path), PLAN_REPO_URL, object())

    assert advisories == []
    warnings = [r for r in caplog.records if r.name == "pipeline" and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].error_type == "RuntimeError"
    assert "abc123" not in warnings[0].getMessage()


def test_should_treat_every_named_repository_as_another_when_plan_url_is_none(listing, tmp_path):
    listing(_listing_answer(REPOS))
    story = _story("Call acme/web first.")

    advisories = story_advisories(story, str(tmp_path), None, object())

    assert [event for event, _ in advisories] == ["cross_repo_unordered"]


def test_should_return_headroom_pair_before_cross_repo_pair(listing, tmp_path):
    _write(tmp_path, "pipeline/near.py", 999)
    story = _story("Call acme/api first.", files=["pipeline/near.py"])

    advisories = story_advisories(story, str(tmp_path), PLAN_REPO_URL, object())

    assert [event for event, _ in advisories] == ["scope_headroom", "cross_repo_unordered"]
    assert "pipeline/near.py" in advisories[0][1]
    assert "github.com/acme/api" in advisories[1][1]


def test_should_return_no_advisories_when_neither_applies(listing, tmp_path):
    story = _story("Nothing to see here.")

    assert story_advisories(story, str(tmp_path), PLAN_REPO_URL, object()) == []


@pytest.fixture
def advisory_plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    return d


def test_ingest_emits_one_cross_repo_notice_and_succeeds(
    advisory_plan_dir, monkeypatch, tmp_path, listing
):
    calls = []
    monkeypatch.setattr(ingest_mod, "_notify_user", lambda *a, **k: calls.append((a, k)))
    repo = tmp_path / "repo"
    repo.mkdir()
    story = {
        "key": "XR-1",
        "summary": "change the api",
        "agent_instructions": PREFLIGHT + "Call github.com/acme/api before editing.",
    }
    plan = {"repo_root": str(repo), "epics": [{"summary": "E1", "stories": [story]}]}
    (advisory_plan_dir / "xrepo1.json").write_text(json.dumps(plan))

    result = p.ingest_plan("xrepo1")

    assert result["ok"] is True
    scoped = [(a, k) for a, k in calls if k.get("event") == "cross_repo_unordered"]
    assert len(scoped) == 1
    assert scoped[0][1]["story_key"] == "XR-1"
    assert "github.com/acme/api" in scoped[0][0][1]


def test_ingest_py_stays_within_the_line_limit():
    lines = Path(ingest_mod.__file__).read_bytes().splitlines()

    assert len(lines) <= 999
