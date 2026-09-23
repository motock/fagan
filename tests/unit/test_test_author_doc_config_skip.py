"""LDC-4: the test-author phase is skipped for stories whose declared files
are all doc or config.

A structural test cannot meaningfully grade a pure doc/config edit before the
change exists, so such a story gets its one small test from the brief instead
of a test-author oracle. The skip must fire BEFORE any backend is resolved or
fetched, must notify the operator, and must not swallow the existing
``[no-new-tests]`` opt-out path.
"""
from pipeline import test_author


def test_all_doc_files_is_doc_or_config_only():
    assert test_author._story_is_doc_or_config_only({"files": ["README.md"]})


def test_mixed_doc_and_code_is_not_doc_or_config_only():
    assert not test_author._story_is_doc_or_config_only(
        {"files": ["README.md", "pipeline/x.py"]}
    )


def test_empty_files_list_is_not_doc_or_config_only():
    assert not test_author._story_is_doc_or_config_only({"files": []})


def test_missing_files_key_is_not_doc_or_config_only():
    assert not test_author._story_is_doc_or_config_only({})


def test_none_files_is_not_doc_or_config_only():
    assert not test_author._story_is_doc_or_config_only({"files": None})


def test_string_files_is_not_doc_or_config_only():
    # A bare string is an undeclared-list scope, not a one-file list; it must
    # not be iterated character-by-character.
    assert not test_author._story_is_doc_or_config_only({"files": "README.md"})


def test_non_str_entry_is_not_doc_or_config_only():
    assert not test_author._story_is_doc_or_config_only({"files": [1]})


def test_suffix_match_is_case_insensitive():
    assert test_author._story_is_doc_or_config_only({"files": ["A.YAML"]})


def test_doc_only_story_skips_phase_and_never_fetches_backend(monkeypatch):
    notifications = []
    backend_calls = []
    monkeypatch.setattr(
        test_author,
        "_notify_user",
        lambda plan, msg, **kwargs: notifications.append((plan, msg)),
    )
    monkeypatch.setattr(
        test_author.backend,
        "get_backend",
        lambda *a, **k: backend_calls.append((a, k)),
    )

    result = test_author._run_test_author_phase(
        {"files": ["REFERENCE.md"], "agent_instructions": "x"},
        story_key="S1",
        worktree_path=test_author.Path("/tmp"),
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
        plan_name="plan",
    )

    assert result is False, "the fail-open contract must be preserved"
    assert notifications, "the skip must be operator-visible"
    assert "every declared file is doc/config" in notifications[0][1]
    assert backend_calls == [], "the skip must fire before any backend is fetched"


def test_code_file_story_still_takes_the_no_new_tests_opt_out(monkeypatch):
    notifications = []
    monkeypatch.setattr(
        test_author,
        "_notify_user",
        lambda plan, msg, **kwargs: notifications.append((plan, msg)),
    )
    monkeypatch.setattr(
        test_author.backend,
        "get_backend",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("backend fetched")),
    )

    result = test_author._run_test_author_phase(
        {
            "files": ["pipeline/x.py"],
            "agent_instructions": "refactor it [no-new-tests]",
        },
        story_key="S2",
        worktree_path=test_author.Path("/tmp"),
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
        plan_name="plan",
    )

    assert result is False
    assert notifications, "the opt-out must stay operator-visible"
    assert "[no-new-tests]" in notifications[0][1]