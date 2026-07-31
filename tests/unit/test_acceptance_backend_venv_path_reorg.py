from pathlib import Path

from app import backend as b


def _repo_root() -> Path:
    return Path(b.__file__).resolve().parent.parent


# .venv/ is gitignored, so unlike scripts/local_agent*.py it never exists in a
# fresh CI checkout -- no on-disk exists() check for _VENV_PYTHON, only the
# structural equality below.
def test_venv_python_points_at_repo_root_not_app_dir():
    assert b.OllamaDriver._VENV_PYTHON == _repo_root() / ".venv" / "bin" / "python3"


def test_agent_script_points_at_repo_root_scripts_dir():
    assert b.OllamaDriver._AGENT_SCRIPT == _repo_root() / "scripts" / "local_agent.py"


def test_agent_script_oracle_points_at_repo_root_scripts_dir():
    assert (
        b.OllamaDriver._AGENT_SCRIPT_ORACLE
        == _repo_root() / "scripts" / "local_agent_oracle.py"
    )


def test_agent_script_paths_exist_on_disk():
    assert b.OllamaDriver._AGENT_SCRIPT.exists()
    assert b.OllamaDriver._AGENT_SCRIPT_ORACLE.exists()
