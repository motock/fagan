"""Tests for the process_scoped field of effective_env_config."""
from tests.unit._config_provenance_helpers import _import_module


class TestEffectiveEnvConfigProcessScoped:
    """`effective` is what THIS process resolves; `process_scoped` says when a
    launcher layer would make the deployment's value differ."""

    @staticmethod
    def _entry(name, **envs):
        mod = _import_module()
        result = mod.effective_env_config(
            environ=envs.get("environ", {}),
            plist_env=envs.get("plist_env", {}),
            mcp_env=envs.get("mcp_env", {}),
        )
        return next(e for e in result if e["name"] == name)

    def test_marks_code_default_as_process_scoped_when_plist_differs(self):
        entry = self._entry("PIPELINE_LOCAL_MAX_STEPS", plist_env={"PIPELINE_LOCAL_MAX_STEPS": "60"})
        assert entry["effective"] == "40"
        assert entry["source"] == "code_default"
        assert entry["process_scoped"] is True

    def test_plist_value_stays_discoverable_in_layers(self):
        entry = self._entry("PIPELINE_LOCAL_MAX_STEPS", plist_env={"PIPELINE_LOCAL_MAX_STEPS": "60"})
        assert {"layer": "launchd_plist", "value": "60"}.items() <= next(
            layer for layer in entry["layers"] if layer["layer"] == "launchd_plist"
        ).items()

    def test_marks_code_default_as_process_scoped_when_mcp_env_differs(self):
        entry = self._entry("PIPELINE_LOCAL_MAX_STEPS", mcp_env={"PIPELINE_LOCAL_MAX_STEPS": "60"})
        assert entry["process_scoped"] is True

    def test_not_process_scoped_when_process_env_sets_var(self):
        entry = self._entry(
            "PIPELINE_LOCAL_MAX_STEPS",
            environ={"PIPELINE_LOCAL_MAX_STEPS": "60"},
            plist_env={"PIPELINE_LOCAL_MAX_STEPS": "60"},
        )
        assert entry["process_scoped"] is False

    def test_not_process_scoped_when_no_launcher_layer_present(self):
        entry = self._entry("PIPELINE_LOCAL_MAX_STEPS")
        assert entry["process_scoped"] is False

    def test_not_process_scoped_when_launcher_value_equals_default(self):
        entry = self._entry("PIPELINE_LOCAL_MAX_STEPS", plist_env={"PIPELINE_LOCAL_MAX_STEPS": "40"})
        assert entry["process_scoped"] is False

    def test_secret_stays_masked_when_process_scoped(self):
        entry = self._entry(
            "PIPELINE_NOTIFY_EMAIL_PASSWORD",
            plist_env={"PIPELINE_NOTIFY_EMAIL_PASSWORD": "hunter2"},
        )
        assert entry["process_scoped"] is True
        assert "hunter2" not in repr(entry)

    def test_entry_count_return_type_and_existing_fields_preserved(self):
        mod = _import_module()
        result = mod.effective_env_config(environ={}, plist_env={}, mcp_env={})
        assert isinstance(result, list)
        assert [e["name"] for e in result] == sorted(s.name for s in mod.ENV_VAR_CATALOG)
        assert {"name", "effective", "source", "restart_required", "conflict",
                "masked", "layers"} <= set(result[0])
