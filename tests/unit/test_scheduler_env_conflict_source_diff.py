"""Tests for the source-diff rewrite of the scheduler env-conflict guard.

``pipeline.scheduler_daemon._report_env_conflicts()`` must diff the launchd
plist against the MCP server's ``~/.claude.json`` env block *directly* (via
``read_plist_env`` / ``read_mcp_server_env``) instead of filtering
``config_provenance.effective_env_config()``, whose universe is the
hand-maintained ``ENV_VAR_CATALOG``.  Only that way can it see divergences
outside the catalog, e.g. ``PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS``
(plist 8100 vs MCP 5400) and ``PIPELINE_AUTO_TRIAGE`` (plist 1, MCP unset).

The plist and ``~/.claude.json`` are the external boundary: fixtures are
written into ``tmp_path`` and pointed at with
``PIPELINE_SCHEDULER_PLIST_PATH`` / ``PIPELINE_CLAUDE_JSON_PATH``.
"""
from tests.unit._config_provenance_helpers import _write_json, _write_plist


def _import_daemon():
    import pipeline.scheduler_daemon as mod

    return mod


def _point_at_fixtures(monkeypatch, tmp_path, plist_env, mcp_env):
    plist = _write_plist(tmp_path, plist_env)
    claude = _write_json(tmp_path, {"mcpServers": {"pipeline": {"env": mcp_env}}})
    monkeypatch.setenv("PIPELINE_SCHEDULER_PLIST_PATH", str(plist))
    monkeypatch.setenv("PIPELINE_CLAUDE_JSON_PATH", str(claude))
    return plist, claude


def _stderr_lines(capsys):
    return [line for line in capsys.readouterr().err.splitlines() if line.strip()]


class TestLiveDivergencesOutsideTheCatalog:
    """The two concrete divergences this story exists to surface."""

    def test_should_warn_for_dispatch_timeout_divergence(
        self, tmp_path, monkeypatch, capsys
    ):
        name = "PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS"
        monkeypatch.delenv(name, raising=False)
        _point_at_fixtures(monkeypatch, tmp_path, {name: "8100"}, {name: "5400"})
        _import_daemon()._report_env_conflicts()
        lines = _stderr_lines(capsys)
        assert len(lines) == 1
        assert lines[0] == (
            f"scheduler_daemon: WARNING {name} differs between config layers: "
            "launchd_plist=8100 mcp_server_env=5400 "
            "(this scheduler uses launchd_plist)"
        )

    def test_should_warn_for_auto_triage_set_only_in_plist(
        self, tmp_path, monkeypatch, capsys
    ):
        name = "PIPELINE_AUTO_TRIAGE"
        monkeypatch.delenv(name, raising=False)
        _point_at_fixtures(monkeypatch, tmp_path, {name: "1"}, {})
        _import_daemon()._report_env_conflicts()
        lines = _stderr_lines(capsys)
        assert len(lines) == 1
        assert lines[0].startswith(f"scheduler_daemon: WARNING {name} ")
        assert "launchd_plist=1" in lines[0]
        assert "mcp_server_env=<unset>" in lines[0]

    def test_should_warn_for_tdd_split_set_only_in_mcp(
        self, tmp_path, monkeypatch, capsys
    ):
        name = "PIPELINE_TDD_SPLIT"
        monkeypatch.delenv(name, raising=False)
        _point_at_fixtures(monkeypatch, tmp_path, {}, {name: "on"})
        _import_daemon()._report_env_conflicts()
        lines = _stderr_lines(capsys)
        assert len(lines) == 1
        assert lines[0].startswith(f"scheduler_daemon: WARNING {name} ")
        assert "launchd_plist=<unset>" in lines[0]
        assert "mcp_server_env=on" in lines[0]


class TestGuardDoesNotDependOnTheCatalog:
    def test_should_warn_even_when_effective_env_config_is_broken(
        self, tmp_path, monkeypatch, capsys
    ):
        """The guard must read the two sources itself; a broken catalog
        enumerator must not silence (or crash) it."""
        import pipeline.config_provenance as provenance

        name = "PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS"
        monkeypatch.delenv(name, raising=False)
        _point_at_fixtures(monkeypatch, tmp_path, {name: "8100"}, {name: "5400"})

        def _boom(*args, **kwargs):
            raise AssertionError("guard must not call effective_env_config()")

        monkeypatch.setattr(provenance, "effective_env_config", _boom)
        _import_daemon()._report_env_conflicts()
        lines = _stderr_lines(capsys)
        assert len(lines) == 1
        assert name in lines[0]
        assert "conflict check failed" not in lines[0]


class TestMasking:
    def test_should_mask_secret_set_only_in_plist(self, tmp_path, monkeypatch, capsys):
        name = "PIPELINE_NOTIFY_EMAIL_PASSWORD"
        monkeypatch.delenv(name, raising=False)
        _point_at_fixtures(monkeypatch, tmp_path, {name: "plist-secret-value"}, {})
        _import_daemon()._report_env_conflicts()
        lines = _stderr_lines(capsys)
        assert len(lines) == 1
        assert name in lines[0]
        assert "launchd_plist=***" in lines[0]
        assert "plist-secret-value" not in lines[0]

    def test_should_mask_secret_set_only_in_mcp(self, tmp_path, monkeypatch, capsys):
        name = "PIPELINE_NOTIFY_EMAIL_PASSWORD"
        monkeypatch.delenv(name, raising=False)
        _point_at_fixtures(monkeypatch, tmp_path, {}, {name: "mcp-secret-value"})
        _import_daemon()._report_env_conflicts()
        lines = _stderr_lines(capsys)
        assert len(lines) == 1
        assert name in lines[0]
        assert "mcp_server_env=***" in lines[0]
        assert "mcp-secret-value" not in lines[0]


class TestBoundaries:
    def test_should_stay_silent_and_not_raise_on_missing_files(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("PIPELINE_SCHEDULER_PLIST_PATH", str(tmp_path / "nope.plist"))
        monkeypatch.setenv("PIPELINE_CLAUDE_JSON_PATH", str(tmp_path / "nope.json"))
        _import_daemon()._report_env_conflicts()  # must not raise
        assert _stderr_lines(capsys) == []

    def test_should_stay_silent_and_not_raise_on_malformed_files(
        self, tmp_path, monkeypatch, capsys
    ):
        plist = tmp_path / "scheduler.plist"
        plist.write_bytes(b"not a plist at all")
        claude = tmp_path / "claude.json"
        claude.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("PIPELINE_SCHEDULER_PLIST_PATH", str(plist))
        monkeypatch.setenv("PIPELINE_CLAUDE_JSON_PATH", str(claude))
        _import_daemon()._report_env_conflicts()  # must not raise
        assert _stderr_lines(capsys) == []

    def test_should_stay_silent_when_both_layers_empty(
        self, tmp_path, monkeypatch, capsys
    ):
        _point_at_fixtures(monkeypatch, tmp_path, {}, {})
        _import_daemon()._report_env_conflicts()
        assert _stderr_lines(capsys) == []

    def test_should_warn_once_per_var_when_many_diverge(
        self, tmp_path, monkeypatch, capsys
    ):
        """Exactly one line per diverging var - the guard must use a single
        code path, not the catalog loop plus a diff loop."""
        names = [
            "PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS",
            "PIPELINE_AUTO_TRIAGE",
            "PIPELINE_TDD_SPLIT",
            "PIPELINE_DECOMPOSE",
        ]
        for name in names:
            monkeypatch.delenv(name, raising=False)
        _point_at_fixtures(
            monkeypatch,
            tmp_path,
            {
                "PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS": "8100",
                "PIPELINE_AUTO_TRIAGE": "1",
            },
            {
                "PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS": "5400",
                "PIPELINE_TDD_SPLIT": "on",
                "PIPELINE_DECOMPOSE": "local",
            },
        )
        _import_daemon()._report_env_conflicts()
        lines = _stderr_lines(capsys)
        assert len(lines) == 4
        for name in names:
            assert sum(1 for line in lines if name in line) == 1, name
