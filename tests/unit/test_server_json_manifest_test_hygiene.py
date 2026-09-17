"""Regression tests for the manifest test-suite hygiene bug raised in review.

Review feedback (REQUEST_CHANGES) on the ``server.json`` story reported a
leftover duplicate test module, ``tests/unit/test_server_json_length_new.py``,
that resolved the repo-root manifest from the *process* CWD::

    with Path("server.json").open("r", encoding="utf-8") as fh:

so it raised ``FileNotFoundError`` whenever pytest was invoked from anywhere
other than the repo root. That is the exact bug class ``pyproject.toml``
documents and that
``tests/unit/test_server_json_length.py::test_manifest_is_found_when_pytest_runs_from_a_foreign_cwd``
exists to guard. The reviewer's fix is to delete that duplicate and keep the
``maxLength: 100`` bound in the canonical contract module
(``tests/unit/test_server_json.py``), which resolves repo-root files from
``__file__``.

These tests are red until the duplicate is deleted. They never load
``server.json`` themselves, so they are CWD-independent by construction.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

TESTS_UNIT_DIR = Path(__file__).resolve().parent
DUPLICATE_MODULE_PATH = TESTS_UNIT_DIR / "test_server_json_length_new.py"


def test_leftover_duplicate_manifest_test_module_is_deleted():
    """The CWD-relative duplicate must not exist in the tree.

    ``test_server_json_length_new.py`` duplicates the description-length
    assertion that belongs in the canonical contract module, but resolves the
    manifest relative to the process CWD instead of ``__file__``. It therefore
    raises ``FileNotFoundError`` for any pytest invocation whose working
    directory is not the repo root.
    """
    assert not DUPLICATE_MODULE_PATH.exists(), (
        f"{DUPLICATE_MODULE_PATH.name} is a leftover duplicate of the canonical "
        "manifest contract tests. It resolves server.json relative to the "
        "process CWD, so it raises FileNotFoundError whenever pytest is invoked "
        "from anywhere but the repo root. Delete it and keep the maxLength-100 "
        "assertion in tests/unit/test_server_json.py, which resolves repo-root "
        "files from __file__."
    )


def test_leftover_duplicate_does_not_raise_filenotfounderror_from_foreign_cwd(
    tmp_path, monkeypatch
):
    """Behavioural reproduction of the reviewer's bug.

    Runs the duplicate module's own tests from a foreign working directory.
    Today that raises ``FileNotFoundError: [Errno 2] No such file or
    directory: 'server.json'`` - the exact failure the reviewer described.
    Once the duplicate is deleted there is nothing left to reproduce and this
    test passes.
    """
    if not DUPLICATE_MODULE_PATH.exists():
        return

    monkeypatch.chdir(tmp_path)

    spec = importlib.util.spec_from_file_location(
        "_server_json_length_new_under_test", DUPLICATE_MODULE_PATH
    )
    assert spec is not None and spec.loader is not None, (
        f"could not load {DUPLICATE_MODULE_PATH}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    test_functions = [
        getattr(module, name)
        for name in sorted(dir(module))
        if name.startswith("test_") and callable(getattr(module, name))
    ]
    assert test_functions, f"{DUPLICATE_MODULE_PATH.name} defines no test functions"

    for test_function in test_functions:
        try:
            test_function()
        except FileNotFoundError as exc:
            raise AssertionError(
                f"{DUPLICATE_MODULE_PATH.name}::{test_function.__name__} resolves "
                f"server.json from the process CWD and raised FileNotFoundError "
                f"when run from {tmp_path}: {exc!r}. Delete the duplicate and "
                f"keep the maxLength-100 assertion in test_server_json.py."
            ) from exc
