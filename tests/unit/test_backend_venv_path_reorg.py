from pathlib import Path

from app import backend as b


def _repo_root() -> Path:
    return Path(b.__file__).resolve().parent.parent


def test_agent_script_equals_repo_root_scripts_local_agent():
    assert (
        b.OllamaDriver._AGENT_SCRIPT
        == Path(b.__file__).resolve().parent.parent / "scripts" / "local_agent.py"
    )


def test_agent_script_oracle_equals_repo_root_scripts_local_agent_oracle():
    assert (
        b.OllamaDriver._AGENT_SCRIPT_ORACLE
        == Path(b.__file__).resolve().parent.parent / "scripts" / "local_agent_oracle.py"
    )


def test_venv_python_equals_repo_root_venv_bin_python3():
    assert (
        b.OllamaDriver._VENV_PYTHON
        == Path(b.__file__).resolve().parent.parent / ".venv" / "bin" / "python3"
    )


def test_agent_script_is_not_under_app_directory():
    app_dir = Path(b.__file__).resolve().parent
    assert app_dir not in b.OllamaDriver._AGENT_SCRIPT.parents


def test_agent_script_oracle_is_not_under_app_directory():
    app_dir = Path(b.__file__).resolve().parent
    assert app_dir not in b.OllamaDriver._AGENT_SCRIPT_ORACLE.parents


def test_venv_python_is_not_under_app_directory():
    app_dir = Path(b.__file__).resolve().parent
    assert app_dir not in b.OllamaDriver._VENV_PYTHON.parents


def test_agent_script_exists_on_disk():
    assert b.OllamaDriver._AGENT_SCRIPT.exists()


def test_agent_script_oracle_exists_on_disk():
    assert b.OllamaDriver._AGENT_SCRIPT_ORACLE.exists()


# No on-disk exists() check for _VENV_PYTHON: .venv/ is gitignored and never
# created in a fresh CI checkout (CI installs deps into the setup-python
# interpreter directly). The equality tests above already pin the correct
# structural path.
