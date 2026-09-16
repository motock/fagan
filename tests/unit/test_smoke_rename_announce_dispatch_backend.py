"""PN-2: the smoke's dispatch guard is renamed to ``_announce_dispatch_backend``.

PURE RENAME story. The prerequisite story (PN-1) changed what the guard DOES -
it now ANNOUNCES the resolved dispatch provider and proceeds, instead of
refusing everything but ``claude`` - which left the old name misleading. This
story only fixes the name; no conditional, return value, exit code or printed
message may change.

These tests grade the rename mechanically:

* the script defines the new name and no longer defines the old one;
* the script's top-level defs are exactly the four allowed names;
* every call site (``run_smoke()`` and ``main()``) uses the new name;
* no source file anywhere in the repo still mentions the old name;
* the renamed function is the SAME behavior (returns the validated triple,
  still exits 2 on an unusable value, still announces a declared provider) -
  a rename, not a rewrite.

The old identifier is assembled from fragments so this file itself never
contains the literal (the repo-wide scan below would otherwise flag it).
"""

import ast
import importlib.util
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "smoke_getting_started.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

NEW_NAME = "_announce_dispatch_backend"
# Assembled from fragments on purpose: the literal must not appear in this file.
OLD_NAME = "_require" + "_claude" + "_backend"

# The exact top-level def set the story pins (order-insensitive).
EXPECTED_TOP_LEVEL_FUNCTIONS = [
    "_announce_dispatch_backend",
    "_prepare_scratch_env",
    "main",
    "run_smoke",
]

# Directories that never hold repo source (build caches, vendored deps, VCS).
_SKIP_DIRS = {
    ".git",
    ".venv",
    ".venv-mlx",
    "__pycache__",
    "node_modules",
    ".local",
    ".pytest_cache",
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _load_script():
    """Import scripts/smoke_getting_started.py as a module, or fail loudly."""
    if not SCRIPT_PATH.exists():
        pytest.fail(f"scripts/smoke_getting_started.py not found at {SCRIPT_PATH}.")
    mod_name = "smoke_getting_started_pn2_rename"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _top_level_function_names(source):
    tree = ast.parse(source)
    return sorted(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def _iter_source_files():
    """Every non-cache, non-log file under the repo root (source + docs)."""
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        name = path.name
        if name.endswith((".pyc", ".log", ".log.ts")):
            continue
        if name.startswith(".agent"):
            continue
        yield path


# --------------------------------------------------------------------------
# the rename itself
# --------------------------------------------------------------------------
def test_script_defines_the_new_name():
    mod = _load_script()
    assert hasattr(mod, NEW_NAME), (
        f"scripts/smoke_getting_started.py must define {NEW_NAME!r} "
        "(the guard was renamed; the old name is now misleading)"
    )
    assert callable(getattr(mod, NEW_NAME)), f"{NEW_NAME!r} must be callable"


def test_script_no_longer_defines_the_old_name():
    mod = _load_script()
    assert not hasattr(mod, OLD_NAME), (
        f"the old identifier {OLD_NAME!r} must be gone from the module "
        "namespace - this is a rename, not an alias"
    )


def test_old_name_is_not_a_top_level_def_in_the_source():
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert OLD_NAME not in _top_level_function_names(source), (
        f"{OLD_NAME!r} must not remain as a top-level def in the script"
    )


def test_top_level_function_names_are_exactly_the_four_allowed():
    """The script's top-level defs are exactly the four pinned names."""
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert _top_level_function_names(source) == EXPECTED_TOP_LEVEL_FUNCTIONS, (
        "the script's top-level defs must be exactly "
        f"{EXPECTED_TOP_LEVEL_FUNCTIONS}; got "
        f"{_top_level_function_names(source)}"
    )


def test_new_name_appears_in_the_script_source():
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert NEW_NAME in source, (
        f"the script source must mention {NEW_NAME!r} (the def and its call "
        "sites)"
    )


def test_script_source_has_no_old_identifier_anywhere():
    """No def, call site, docstring or comment may keep the old name."""
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert OLD_NAME not in source, (
        f"the old identifier {OLD_NAME!r} must not appear anywhere in "
        "scripts/smoke_getting_started.py (def, call sites, docstrings or "
        "comments)"
    )


# --------------------------------------------------------------------------
# call sites in run_smoke() and main()
# --------------------------------------------------------------------------
def test_run_smoke_calls_the_new_name():
    mod = _load_script()
    source = inspect.getsource(mod.run_smoke)
    assert NEW_NAME in source, (
        f"run_smoke() must call {NEW_NAME!r} (its call site was renamed)"
    )
    assert OLD_NAME not in source, f"run_smoke() must not call {OLD_NAME!r}"


def test_main_calls_the_new_name():
    mod = _load_script()
    source = inspect.getsource(mod.main)
    assert NEW_NAME in source, (
        f"main() must call {NEW_NAME!r} (its call site was renamed)"
    )
    assert OLD_NAME not in source, f"main() must not call {OLD_NAME!r}"


# --------------------------------------------------------------------------
# repo-wide: the old name is gone from every source file (incl. docs)
# --------------------------------------------------------------------------
def test_no_source_file_in_the_repo_mentions_the_old_name():
    needle = OLD_NAME.encode()
    offenders = []
    for path in _iter_source_files():
        try:
            data = path.read_bytes()
        except OSError:  # pragma: no cover - unreadable file, not our concern
            continue
        if needle in data:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], (
        f"the old identifier {OLD_NAME!r} must be gone from the whole repo "
        f"(including docs); still present in: {offenders}"
    )


def test_no_tracked_file_mentions_the_old_name():
    """Same scan, restricted to git-tracked files (the committed surface)."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    needle = OLD_NAME.encode()
    offenders = []
    for rel in result.stdout.splitlines():
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:  # pragma: no cover
            continue
        if needle in data:
            offenders.append(rel)
    assert offenders == [], (
        f"no tracked file may mention {OLD_NAME!r}; still present in: "
        f"{offenders}"
    )


# --------------------------------------------------------------------------
# behavior is UNCHANGED by the rename
# --------------------------------------------------------------------------
def test_renamed_guard_returns_the_validated_triple():
    """The guard still returns the (provider, model, source) triple verbatim."""
    mod = _load_script()
    triple = ("claude", "sonnet", "env var PIPELINE_BACKEND_DISPATCH")
    assert getattr(mod, NEW_NAME)(resolver=lambda: triple) == triple


def test_renamed_guard_still_exits_2_on_unusable_values():
    """Empty/whitespace-only and unknown values still fail closed with exit 2."""
    mod = _load_script()
    for value in ("", "   ", "bogus"):
        with pytest.raises(SystemExit) as excinfo:
            getattr(mod, NEW_NAME)(value)
        assert excinfo.value.code == 2, (
            f"the renamed guard must still exit 2 for {value!r}, got "
            f"{excinfo.value.code!r}"
        )


def test_renamed_guard_still_announces_a_declared_provider(monkeypatch, capsys):
    """A declared provider still passes and is announced (PN-1 behavior kept)."""
    mod = _load_script()
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "ollama")
    getattr(mod, NEW_NAME)()  # must NOT raise
    captured = capsys.readouterr()
    text = (captured.out + captured.err).lower()
    assert "ollama" in text, (
        "the renamed guard must still announce the resolved provider; got "
        f"stdout={captured.out!r} stderr={captured.err!r}"
    )


def test_renamed_guard_rejection_message_is_unchanged(capsys):
    """The exit-2 message still names the value and lists the providers."""
    mod = _load_script()
    with pytest.raises(SystemExit):
        getattr(mod, NEW_NAME)("bogus")
    captured = capsys.readouterr()
    message = captured.out + captured.err
    assert "bogus" in message, (
        f"the rejection message must still name the offending value; got: "
        f"{message!r}"
    )
    for provider in ("claude", "ollama", "lmstudio", "mlx", "local", "auto"):
        assert provider in message.lower(), (
            "the rejection message must still list the recognised providers "
            f"(missing {provider!r}); got: {message!r}"
        )
