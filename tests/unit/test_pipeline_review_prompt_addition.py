from app import pipeline_mcp_server as p


def test_run_reviewer_includes_new_check_and_preserves_existing(monkeypatch):
    captured = {}
    class _FakeDriver:
        def complete(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return "VERDICT: APPROVE"
    monkeypatch.setattr(p.backend, "get_backend", lambda role, name=None: _FakeDriver())
    p._run_reviewer("/tmp/some-worktree", "agent/some-branch")
    prompt = captured["prompt"]
    # Check new check present
    assert "(4) if the change adds a module" in prompt
    # Ensure existing numbered checks remain
    for num in ["(1)", "(2)", "(3)"]:
        assert num in prompt
