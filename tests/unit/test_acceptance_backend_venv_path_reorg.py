from pathlib import Path

from app import backend as b


def _repo_root() -> Path:
    return Path(b.__file__).resolve().parent.parent


def test_venv_python_points_at_repo_root_not_app_dir():
    assert b.OllamaDriver._VENV_PYTHON == _repo_root() / ".venv" / "bin" / "python3"


def test_agent_script_points_at_repo_root_scripts_dir():
    assert b.OllamaDriver._AGENT_SCRIPT == _repo_root() / "scripts" / "local_agent.py"


def test_agent_script_oracle_points_at_repo_root_scripts_dir():
    assert (
        b.OllamaDriver._AGENT_SCRIPT_ORACLE
        == _repo_root() / "scripts" / "local_agent_oracle.py"
    )


def test_venv_python_path_actually_exists_on_disk():
    assert b.OllamaDriver._VENV_PYTHON.exists(), (
        f"{b.OllamaDriver._VENV_PYTHON} does not exist on disk -- dispatch_story "
        f"would fail spawning the local agent subprocess with this path"
    )


def test_agent_script_paths_exist_on_disk():
    assert b.OllamaDriver._AGENT_SCRIPT.exists()
    assert b.OllamaDriver._AGENT_SCRIPT_ORACLE.exists()
