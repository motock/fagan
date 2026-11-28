"""Test-seam baseline: patches on pipeline.server, reads routed back through
it, and inspect.getsource calls may only shrink.

Compares per-key maps against ``test_seam_baseline.json``; never asserts totals.
After removing seams, run ``python scripts/test_seam_inventory.py --write``.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = Path(__file__).with_name("test_seam_baseline.json")
_MAPS = ("patch_sites", "server_reads", "getsource_calls")


def _load_inventory():
    spec = importlib.util.spec_from_file_location(
        "test_seam_inventory", REPO_ROOT / "scripts" / "test_seam_inventory.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inv = _load_inventory()


@pytest.fixture(scope="module")
def live():
    return inv.measure(REPO_ROOT)


@pytest.fixture(scope="module")
def baseline():
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def test_no_seam_count_grows_or_appears(live, baseline):
    problems = [
        f"{name}[{key}]: {count} > baseline {baseline[name].get(key, 0)}"
        for name in _MAPS
        for key, count in live[name].items()
        if count > baseline[name].get(key, 0)
    ]
    assert not problems, "test seams grew:\n" + "\n".join(problems)


def test_baseline_is_not_stale(live, baseline):
    problems = [
        f"{name}[{key}]: {live[name].get(key, 0)} < baseline {count}"
        for name in _MAPS
        for key, count in baseline[name].items()
        if live[name].get(key, 0) < count
    ]
    assert not problems, (
        "baseline is stale, run `python scripts/test_seam_inventory.py --write`:\n"
        + "\n".join(problems)
    )


class TestPatchSites:
    def test_counts_aliased_import(self):
        src = (
            "import pipeline.server as p\n"
            "def t(monkeypatch):\n"
            "    monkeypatch.setattr(p, 'PLAN_DIR', 1)\n"
            "    monkeypatch.setattr(p, 'PLAN_DIR', 2)\n"
        )
        assert inv.count_patch_sites(src) == {"PLAN_DIR": 2}

    def test_counts_from_import_alias_and_patch_object(self):
        src = (
            "from pipeline import server as srv\n"
            "from unittest.mock import patch\n"
            "def t():\n"
            "    with patch.object(srv, '_store'):\n"
            "        pass\n"
        )
        assert inv.count_patch_sites(src) == {"_store": 1}

    def test_counts_string_form(self):
        src = (
            "from unittest.mock import patch\n"
            "def t(monkeypatch):\n"
            "    with patch('pipeline.server._merge_pr'):\n"
            "        monkeypatch.setattr('pipeline.server.PLAN_DIR', 1)\n"
        )
        assert inv.count_patch_sites(src) == {"_merge_pr": 1, "PLAN_DIR": 1}

    def test_ignores_other_modules(self):
        src = (
            "import pipeline.store as s\n"
            "from unittest.mock import patch\n"
            "def t(monkeypatch):\n"
            "    monkeypatch.setattr(s, 'PLAN_DIR', 1)\n"
            "    with patch('pipeline.store.PLAN_DIR'):\n"
            "        pass\n"
        )
        assert inv.count_patch_sites(src) == {}

    def test_syntax_error_is_raised(self):
        with pytest.raises(SyntaxError):
            inv.count_patch_sites("def (:\n")


class TestServerReads:
    def test_counts_function_level_imports(self):
        src = (
            "def f():\n"
            "    from .server import _store, _notify_user\n"
            "    from pipeline.server import _store\n"
        )
        assert inv.count_server_reads(src) == {"_store": 2, "_notify_user": 1}

    def test_counts_ref_declarations(self):
        src = 'a = LiveRef("PLAN_DIR")\nb = _ServerRef("_store")\n'
        assert inv.count_server_reads(src) == {"PLAN_DIR": 1, "_store": 1}

    def test_module_top_import_not_counted(self):
        assert inv.count_server_reads("from .server import _store\n") == {}

    def test_other_module_import_not_counted(self):
        assert inv.count_server_reads("def f():\n    from .store import _store\n") == {}

    def test_syntax_error_is_raised(self):
        with pytest.raises(SyntaxError):
            inv.count_server_reads("def (:\n")


class TestGetsourceCalls:
    def test_counts_both_functions(self):
        src = "import inspect\nx = inspect.getsource(f)\ny = inspect.getsourcelines(f)\n"
        assert inv.count_getsource_calls(src) == 2

    def test_counts_from_import_and_alias(self):
        src = "import inspect as i\nfrom inspect import getsource as gs\ni.getsource(f)\ngs(f)\n"
        assert inv.count_getsource_calls(src) == 2

    def test_ignores_unrelated_getsource(self):
        assert inv.count_getsource_calls("other.getsource(f)\ninspect.signature(f)\n") == 0

    def test_syntax_error_is_raised(self):
        with pytest.raises(SyntaxError):
            inv.count_getsource_calls("def (:\n")
