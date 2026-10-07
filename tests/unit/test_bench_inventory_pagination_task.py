"""Oracle-quality tests for the inventory_pagination benchmark task.

BPM-6 authors a seeded Tier 3 benchmark task: the agent must turn a
non-paginated ``list_items(limit=100)`` into a cursor-paginated API and fix
``report.count_all()``, which silently under-counts above 100 items. These
tests grade the ORACLE rather than any solution: the hidden ground truth must
fail the unmodified seed (so the task is not already solved), pass a correct
reference solution, and catch a family of subtly-broken references that the
visible acceptance oracle lets through.
"""
import functools
import importlib
import re
import shutil
import subprocess
import sys
import textwrap

import pytest

from tests.benchmark import harness

TASK_NAME = "inventory_pagination"
SEED_DIR = harness.TASKS_DIR / TASK_NAME / "seed"
README = harness.TASKS_DIR.parent / "README.md"

# A correct solution, kept inside this test file so the oracle is graded
# against a known-good implementation rather than against the seed.
REF_API = textwrap.dedent('''\
    """Reference solution for the inventory API."""
    ITEMS = [{"id": i, "name": "item-%03d" % i} for i in range(1, 251)]


    def _decode(cursor):
        try:
            start = int(cursor)
        except (TypeError, ValueError):
            raise ValueError("unknown or malformed cursor")
        if start < 0 or start > len(ITEMS):
            raise ValueError("unknown or malformed cursor")
        return start


    def list_items(cursor=None, limit=50):
        if limit < 1:
            raise ValueError("limit must be a positive integer")
        if limit > 100:
            limit = 100
        start = 0 if cursor is None else _decode(cursor)
        window = ITEMS[start:start + limit]
        end = start + len(window)
        next_cursor = None if end >= len(ITEMS) else str(end)
        return window, next_cursor
    ''')

REF_REPORT = textwrap.dedent('''\
    """Reference solution for the inventory report."""
    from inventory import api


    def count_all():
        total = 0
        cursor = None
        while True:
            items, cursor = api.list_items(cursor=cursor)
            total += len(items)
            if cursor is None:
                return total
    ''')

# Correct against the real 250-item catalog, wrong the moment ITEMS is
# monkeypatched - so the visible acceptance oracle still passes it.
STALE_TOTAL_REPORT = textwrap.dedent('''\
    """Broken reference: the total is captured at import time."""
    from inventory import api

    _TOTAL = len(api.ITEMS)


    def count_all():
        return _TOTAL
    ''')


def _swap(src, old, new, count=1):
    """Replace exactly `count` occurrences of an indentation-free anchor.

    The anchor must never include leading whitespace: a wrong anchor would
    make `.replace` a silent no-op and the "broken" reference would secretly
    be the correct one, so the occurrence count is checked up front.
    """
    found = src.count(old)
    assert found == count, f"anchor {old!r} appears {found}x, expected {count}x"
    return src.replace(old, new)


# (id, api source, report source, does the VISIBLE acceptance oracle pass?)
# ``None`` means the visible oracle may legitimately either catch or miss the
# variant, so only the hidden ground truth is asserted for it.
BROKEN_REFERENCES = [
    ("limit_zero_and_negative_accepted",
     _swap(REF_API, "if limit < 1:", "if False:"), REF_REPORT, True),
    ("limit_one_rejected",
     _swap(REF_API, "if limit < 1:", "if limit < 2:"), REF_REPORT, True),
    ("unknown_cursor_accepted",
     _swap(REF_API, 'raise ValueError("unknown or malformed cursor")',
           "return 0", count=2), REF_REPORT, True),
    ("default_limit_is_100",
     _swap(REF_API, "limit=50", "limit=100"), REF_REPORT, False),
    ("walk_stops_one_page_early",
     _swap(REF_API, "end >= len(ITEMS) else str(end)",
           "end >= len(ITEMS) - limit else str(end)"), REF_REPORT, False),
    ("pages_overlap",
     _swap(REF_API, "else str(end)", "else str(end - 5)"), REF_REPORT, False),
    ("page_items_reversed",
     _swap(REF_API, "window = ITEMS[start:start + limit]",
           "window = list(reversed(ITEMS[start:start + limit]))"),
     REF_REPORT, None),
    ("count_all_ignores_monkeypatched_items",
     REF_API, STALE_TOTAL_REPORT, True),
]


@functools.lru_cache(maxsize=1)
def _task() -> dict:
    return harness.load_task(TASK_NAME)


def _copy_seed(dest):
    shutil.copytree(SEED_DIR, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def _write_reference(dest, api_src=REF_API, report_src=REF_REPORT):
    pkg = dest / "inventory"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "api.py").write_text(api_src)
    (pkg / "report.py").write_text(report_src)
    return dest


def _run_oracle(impl_src, oracle_src, scratch) -> dict:
    task = _task()
    return harness.run_groundtruth(
        impl_src, task["impl_file"], oracle_src, scratch,
        ecosystem=task["ecosystem"],
        extra_impl_files=task["extra_impl_files"],
    )


def _purge_inventory_modules():
    for name in [m for m in sys.modules
                 if m == "inventory" or m.startswith("inventory.")]:
        del sys.modules[name]


def _sections(text):
    """Markdown split into (heading, body) pairs on '## ' headings."""
    out = []
    for part in text.split("\n## ")[1:]:
        heading, _, body = part.partition("\n")
        out.append((heading.strip(), body))
    return out


def test_load_task_declares_the_tier3_contract():
    task = _task()
    assert task["name"] == TASK_NAME
    assert task["tier"] == "T3"
    assert task["impl_file"] == "inventory/report.py"
    assert task["extra_impl_files"] == ["inventory/__init__.py", "inventory/api.py"]
    assert task["persona"] == "software-engineer"
    assert task["model"] == "sonnet"
    assert task["risk"] == "low"
    assert task["ecosystem"] == "pytest"
    for field in ("summary", "agent_instructions"):
        assert isinstance(task[field], str) and task[field].strip()
    instructions = task["agent_instructions"]
    for token in ("list_items", "count_all", "next_cursor", "limit",
                  "ValueError", "README", "test_inventory", "50", "100"):
        assert token in instructions, f"agent_instructions must mention {token}"
    for rel in ("inventory/__init__.py", "inventory/api.py", "inventory/report.py",
                "README.md", "test_inventory.py"):
        assert (SEED_DIR / rel).is_file(), f"seed/{rel} is missing"
        assert rel in task["seed_files"], f"seed_files must contain {rel}"
    seed_readme = (SEED_DIR / "README.md").read_text()
    assert "list_items" in seed_readme and "count_all" in seed_readme


def test_readme_documents_the_tier3_task():
    text = README.read_text()
    paragraphs = [p for p in text.split("\n\n") if "Tier 3" in p]
    assert paragraphs, "README must describe the Tier 3 tier"
    scope = "\n\n".join(paragraphs)
    assert "not yet implemented" not in scope
    assert TASK_NAME in scope
    assert "mock" in scope.lower()
    listed = [body for heading, body in _sections(text)
              if "layout" in heading.lower() or "task" in heading.lower()]
    assert listed, "README must keep a Layout/task-list section"
    assert any(TASK_NAME in body for body in listed)


def test_seed_catalog_is_deterministic_and_count_all_undercounts(tmp_path):
    seed = _copy_seed(tmp_path / "seed")
    _purge_inventory_modules()
    sys.path.insert(0, str(seed))
    try:
        api = importlib.import_module("inventory.api")
        report = importlib.import_module("inventory.report")
        assert len(api.ITEMS) == 250
        snapshot = list(api.ITEMS)
        importlib.reload(api)
        assert list(api.ITEMS) == snapshot
        assert list(api.list_items(limit=5)) == snapshot[:5]
        assert len(list(api.list_items())) == 100
        assert report.count_all() != len(api.ITEMS)
    finally:
        sys.path.remove(str(seed))
        _purge_inventory_modules()


def test_seed_suite_passes_against_the_unmodified_seed(tmp_path):
    seed = _copy_seed(tmp_path / "seed")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "test_inventory.py", "-q",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=str(seed), capture_output=True, text=True, check=False,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-2000:]
    m = re.search(r"(\d+) passed", out)
    assert m is not None, out[-500:]
    assert 3 <= int(m.group(1)) <= 5, out[-500:]


def test_groundtruth_fails_against_the_unmodified_seed(tmp_path):
    seed = _copy_seed(tmp_path / "seed")
    result = _run_oracle(seed, _task()["groundtruth_source"], tmp_path / "gt")
    assert result["ran"] is True, result
    assert result["passed"] is False, result.get("tail", "")


def test_groundtruth_passes_against_a_correct_reference(tmp_path):
    ref = _write_reference(tmp_path / "ref")
    result = _run_oracle(ref, _task()["groundtruth_source"], tmp_path / "gt")
    assert result["ran"] is True, result
    assert result["passed"] is True, result.get("tail", "")


def test_acceptance_passes_on_reference_and_fails_on_seed(tmp_path):
    task = _task()
    ref = _write_reference(tmp_path / "ref")
    ok = _run_oracle(ref, task["acceptance_source"], tmp_path / "acc_ref")
    assert ok["ran"] is True, ok
    assert ok["passed"] is True, ok.get("tail", "")
    seed = _copy_seed(tmp_path / "seed")
    bad = _run_oracle(seed, task["acceptance_source"], tmp_path / "acc_seed")
    assert bad["ran"] is True, bad
    assert bad["passed"] is False, bad.get("tail", "")


def test_reference_without_limit_clamp_fails_groundtruth_but_passes_acceptance(tmp_path):
    task = _task()
    ref = _write_reference(
        tmp_path / "ref", _swap(REF_API, "if limit > 100:", "if False:"))
    gt = _run_oracle(ref, task["groundtruth_source"], tmp_path / "gt")
    assert gt["ran"] is True, gt
    assert gt["passed"] is False, gt.get("tail", "")
    acc = _run_oracle(ref, task["acceptance_source"], tmp_path / "acc")
    assert acc["ran"] is True, acc
    assert acc["passed"] is True, acc.get("tail", "")


@pytest.mark.parametrize(
    ("api_src", "report_src", "acceptance_expected"),
    [pytest.param(a, r, e, id=n) for n, a, r, e in BROKEN_REFERENCES],
)
def test_groundtruth_catches_broken_reference(
        api_src, report_src, acceptance_expected, tmp_path):
    ref = _write_reference(tmp_path / "ref", api_src, report_src)
    gt = _run_oracle(ref, _task()["groundtruth_source"], tmp_path / "gt")
    assert gt["ran"] is True, gt
    assert gt["passed"] is False, gt.get("tail", "")
    if acceptance_expected is None:
        return
    acc = _run_oracle(ref, _task()["acceptance_source"], tmp_path / "acc")
    assert acc["ran"] is True, acc
    assert acc["passed"] is acceptance_expected, acc.get("tail", "")