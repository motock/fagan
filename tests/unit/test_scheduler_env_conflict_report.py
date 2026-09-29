"""Tests for the scheduler-startup env-conflict warning.

``pipeline.scheduler_daemon._report_env_conflicts()`` compares the launchd
plist against the MCP server's ``~/.claude.json`` block and warns on stderr
about every cataloged env var whose value differs between the two layers.

The plist and ``~/.claude.json`` are the external boundary here: fixtures are
written into ``tmp_path`` and pointed at with
``PIPELINE_SCHEDULER_PLIST_PATH`` / ``PIPELINE_CLAUDE_JSON_PATH``.
"""
import inspect

from tests.unit._config_provenance_helpers import _write_json, _write_plist


def _import_daemon():
    import pipeline.scheduler_daemon as mod

    return mod


def _point_at_fixtures(monkeypatch, tmp_path, plist_env, mcp_env):
    """Write plist + claude.json fixtures and point the readers at them."""
    plist = _write_plist(tmp_path, plist_env)
    claude = _write_json(
        tmp_path, {"mcpServers": {"pipeline": {"env": mcp_env}}}
    )
    monkeypatch.setenv("PIPELINE_SCHEDULER_PLIST_PATH", str(plist))
    monkeypatch.setenv("PIPELINE_CLAUDE_JSON_PATH", str(claude))
    return plist, claude


def _stderr_lines(capsys):
    return [line for line in capsys.readouterr().err.splitlines() if line.strip()]


class TestReportEnvConflicts:
    def test_should_warn_once_per_conflicting_var(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("PIPELINE_MAX_CONCURRENT_AGENTS", raising=False)
        _point_at_fixtures(
            monkeypatch,
            tmp_path,
            {"PIPELINE_MAX_CONCURRENT_AGENTS": "2"},
            {"PIPELINE_MAX_CONCURRENT_AGENTS": "4"},
        )
        _import_daemon()._report_env_conflicts()
        lines = _stderr_lines(capsys)
        assert len(lines) == 1
        assert lines[0] == (
            "scheduler_daemon: WARNING PIPELINE_MAX_CONCURRENT_AGENTS differs "
            "between config layers: launchd_plist=2 mcp_server_env=4 "
            "(this scheduler uses launchd_plist)"
        )

    def test_should_warn_once_for_each_of_several_conflicts(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.delenv("PIPELINE_MAX_CONCURRENT_AGENTS", raising=False)
        monkeypatch.delenv("PIPELINE_LOCAL_MODEL_DEFAULT", raising=False)
        _point_at_fixtures(
            monkeypatch,
            tmp_path,
            {
                "PIPELINE_MAX_CONCURRENT_AGENTS": "2",
                "PIPELINE_LOCAL_MODEL_DEFAULT": "devstral:24b",
            },
            {
                "PIPELINE_MAX_CONCURRENT_AGENTS": "4",
                "PIPELINE_LOCAL_MODEL_DEFAULT": "qwen3:32b",
            },
        )
        _import_daemon()._report_env_conflicts()
        lines = _stderr_lines(capsys)
        assert len(lines) == 2
        assert any("PIPELINE_MAX_CONCURRENT_AGENTS" in line for line in lines)
        assert any("PIPELINE_LOCAL_MODEL_DEFAULT" in line for line in lines)
        assert all(line.startswith("scheduler_daemon: WARNING ") for line in lines)

    def test_should_stay_silent_when_layers_agree(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("PIPELINE_MAX_CONCURRENT_AGENTS", raising=False)
        _point_at_fixtures(
            monkeypatch,
            tmp_path,
            {"PIPELINE_MAX_CONCURRENT_AGENTS": "3"},
            {"PIPELINE_MAX_CONCURRENT_AGENTS": "3"},
        )
        _import_daemon()._report_env_conflicts()
        assert _stderr_lines(capsys) == []

    def test_should_warn_when_var_is_set_in_only_one_layer(
        self, tmp_path, monkeypatch, capsys
    ):
        """A key set in only one layer IS a divergence: the two code paths run
        with different behaviour.  The absent side is named as ``<unset>``."""
        monkeypatch.delenv("PIPELINE_MAX_CONCURRENT_AGENTS", raising=False)
        # plist only
        _point_at_fixtures(
            monkeypatch,
            tmp_path,
            {"PIPELINE_MAX_CONCURRENT_AGENTS": "2"},
            {},
        )
        _import_daemon()._report_env_conflicts()
        lines = _stderr_lines(capsys)
        assert len(lines) == 1
        assert lines[0].startswith("scheduler_daemon: WARNING ")
        assert "PIPELINE_MAX_CONCURRENT_AGENTS" in lines[0]
        assert "launchd_plist=2" in lines[0]
        assert "mcp_server_env=<unset>" in lines[0]
        # claude.json only
        _point_at_fixtures(
            monkeypatch,
            tmp_path,
            {},
            {"PIPELINE_MAX_CONCURRENT_AGENTS": "4"},
        )
        _import_daemon()._report_env_conflicts()
        lines = _stderr_lines(capsys)
        assert len(lines) == 1
        assert lines[0].startswith("scheduler_daemon: WARNING ")
        assert "PIPELINE_MAX_CONCURRENT_AGENTS" in lines[0]
        assert "launchd_plist=<unset>" in lines[0]
        assert "mcp_server_env=4" in lines[0]
        # neither layer has anything at all
        _point_at_fixtures(monkeypatch, tmp_path, {}, {})
        _import_daemon()._report_env_conflicts()
        assert _stderr_lines(capsys) == []

    def test_should_stay_silent_for_benign_one_sided_diffs(
        self, tmp_path, monkeypatch, capsys
    ):
        """Every name in the explicit benign allow-list is subtracted from the
        one-sided diff, so it produces no warning."""
        daemon = _import_daemon()
        benign = getattr(daemon, "_BENIGN_ONE_SIDED_DIFFS", None)
        assert isinstance(benign, (set, frozenset))
        assert all(isinstance(name, str) for name in benign)
        for name in sorted(benign):
            monkeypatch.delenv(name, raising=False)
            _point_at_fixtures(monkeypatch, tmp_path, {name: "plist-value"}, {})
            daemon._report_env_conflicts()
            assert _stderr_lines(capsys) == [], name

    def test_should_mask_secret_values(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("PIPELINE_NOTIFY_EMAIL_PASSWORD", raising=False)
        _point_at_fixtures(
            monkeypatch,
            tmp_path,
            {"PIPELINE_NOTIFY_EMAIL_PASSWORD": "plist-secret-value"},
            {"PIPELINE_NOTIFY_EMAIL_PASSWORD": "mcp-secret-value"},
        )
        _import_daemon()._report_env_conflicts()
        lines = _stderr_lines(capsys)
        assert len(lines) == 1
        assert "PIPELINE_NOTIFY_EMAIL_PASSWORD" in lines[0]
        assert "***" in lines[0]
        assert "plist-secret-value" not in lines[0]
        assert "mcp-secret-value" not in lines[0]

    def test_should_not_raise_when_check_fails(self, monkeypatch, capsys):
        import pipeline.config_provenance as provenance

        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(provenance, "read_plist_env", _boom)
        _import_daemon()._report_env_conflicts()  # must not raise
        lines = _stderr_lines(capsys)
        assert len(lines) == 1
        assert "conflict check failed: RuntimeError" in lines[0]


class TestWiring:
    def test_report_is_called_after_lock_and_before_health_path(self):
        daemon = _import_daemon()
        src = inspect.getsource(daemon.run_daemon)
        assert "_report_env_conflicts()" in src
        call_at = src.index("_report_env_conflicts()")
        assert call_at > src.index("fcntl.flock(")
        assert call_at < src.index("health_path =")

    def test_report_is_defined_above_run_daemon(self):
        daemon = _import_daemon()
        module_src = inspect.getsource(daemon)
        assert module_src.index("def _report_env_conflicts(") < module_src.index(
            "def run_daemon("
        )
