"""Gate the Node frontend suites so they actually run in CI.

Why this file exists: the ``test_*.mjs`` / ``test_*.js`` suites under
``tests/`` were run by nothing. ``.github/workflows/ci.yml`` runs only
``pytest`` and ``ruff check .``, and no Python test invoked ``node``, so a
regression test added for the duplicate-reply bug would have been decorative.
This module discovers every Node suite, runs it from its own directory, and
fails the Python gate when one goes red.

The three helpers this file grades -- ``_discover_suites``, ``_run_suite``
and ``_KNOWN_BROKEN`` -- are the module's public surface: the read-only
acceptance oracle ``tests/acceptance_node_suite_gating.py`` imports them by
name. They are resolved through ``globals()`` at call time so this file
collects cleanly (and reports a precise, per-test failure) before they exist.

``_KNOWN_BROKEN`` documents debt instead of hiding it: each entry is a suite
that is red today for a reason outside this story's scope. The ratchet test
below fails the moment one of them starts passing, so the skip list cannot
rot -- the exact failure mode this whole gate exists to fix.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Suites green when this gate was written. Membership only -- never an exact
# total, so a suite added by a later story does not break this file.
KNOWN_GREEN = (
    "tests/test_app_workspace.mjs",
    "tests/test_app_workspace_picker.mjs",
    "tests/test_app_workspace_wiring.mjs",
    "tests/unit/test_comms_sse_stream.mjs",
    "tests/unit/test_markdown_no_interactive_controls.mjs",
    "tests/unit/test_markdown_render.mjs",
    "tests/unit/test_patch_module.mjs",
    "tests/unit/test_patch_ui_wiring.mjs",
    "tests/unit/test_plan_list_scoping.mjs",
    "tests/unit/test_usage_backends_render.mjs",
)

_GATE_HELPERS = ("_discover_suites", "_run_suite", "_KNOWN_BROKEN")


def _helpers():
    """Resolve the gate helpers from this module at call time."""
    missing = [name for name in _GATE_HELPERS if name not in globals()]
    if missing:
        pytest.fail(
            "tests/unit/test_node_suites.py must define "
            + ", ".join(missing)
            + " -- the Node-suite gate helpers this file grades"
        )
    return (
        globals()["_discover_suites"],
        globals()["_run_suite"],
        globals()["_KNOWN_BROKEN"],
    )


def _rel_to(root, path):
    """Repo-relative POSIX key for ``path``, resolved under ``root``."""
    root = Path(root).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve().relative_to(root).as_posix()


def _rel(path):
    return _rel_to(REPO_ROOT, path)


def pytest_generate_tests(metafunc):
    """Parametrize from the live discovery run, never a hardcoded file list."""
    discover = globals().get("_discover_suites")
    known = globals().get("_KNOWN_BROKEN")

    if "suite" in metafunc.fixturenames:
        if discover is None or known is None:
            metafunc.parametrize("suite", [None], ids=["gate-helpers-missing"])
        else:
            green = [p for p in discover(REPO_ROOT) if _rel(p) not in known]
            metafunc.parametrize("suite", green, ids=[_rel(p) for p in green])

    if "broken_suite" in metafunc.fixturenames:
        if known is None:
            metafunc.parametrize(
                "broken_suite", [None], ids=["gate-helpers-missing"]
            )
        else:
            keys = sorted(known)
            metafunc.parametrize("broken_suite", keys, ids=keys)


# --------------------------------------------------------------------------
# node availability: a missing node is a hard failure, never a skip
# --------------------------------------------------------------------------


def test_node_is_available_for_the_frontend_gate():
    """A silently-skipping gate is the failure mode this repo forbids."""
    node = shutil.which("node")
    assert node, (
        "node is required to gate the frontend suites and is not on PATH; "
        "install Node.js. This gate must FAIL, never skip, when node is missing."
    )


def test_gate_fails_hard_when_node_is_missing(tmp_path):
    """Re-run this file with node stripped from PATH: it must go red."""
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    env = dict(os.environ)
    env["PATH"] = str(empty_bin)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(Path(__file__).resolve()),
            "-q",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "-k",
            "node_is_available",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=str(REPO_ROOT),
    )
    output = proc.stdout + proc.stderr
    assert "no tests ran" not in output.lower(), (
        f"the nested gate run collected nothing, so this proves nothing:\n{output}"
    )
    assert proc.returncode != 0, f"gate passed with node missing:\n{output}"
    assert "skipped" not in output.lower(), (
        f"gate skipped instead of failing when node is missing:\n{output}"
    )
    assert "node" in output.lower(), (
        f"the failure must name the missing node binary:\n{output}"
    )


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


def test_discover_suites_finds_the_known_green_suites():
    discover, _, _ = _helpers()
    discovered = {_rel(p) for p in discover(REPO_ROOT)}
    missing = sorted(set(KNOWN_GREEN) - discovered)
    assert not missing, f"_discover_suites missed known-green suites: {missing}"


def test_discover_suites_returns_sorted_pathlib_paths():
    discover, _, _ = _helpers()
    suites = discover(REPO_ROOT)
    assert isinstance(suites, list), f"expected a list, got {type(suites)!r}"
    assert suites, "_discover_suites found no Node suites at all"
    assert all(isinstance(p, Path) for p in suites), "entries must be Path objects"
    assert suites == sorted(suites), "discovery must return a sorted list"


def test_discover_suites_excludes_underscore_prefixed_paths():
    discover, _, _ = _helpers()
    discovered = [_rel(p) for p in discover(REPO_ROOT)]

    leaked = sorted(
        p for p in discovered if any(part.startswith("_") for part in Path(p).parts)
    )
    assert not leaked, f"discovery leaked non-suite paths: {leaked}"

    # The helper really is on disk, so the exclusion above is load-bearing.
    assert (REPO_ROOT / "tests" / "_app_js_loader.mjs").exists()
    assert "tests/_app_js_loader.mjs" not in discovered


def test_discover_suites_is_glob_based_and_skips_scratch_trees(tmp_path):
    """A new suite is picked up; benchmark/_runs scratch copies are not."""
    discover, _, _ = _helpers()

    tests = tmp_path / "tests"
    (tests / "unit").mkdir(parents=True)
    (tests / "unit" / "test_brand_new.mjs").write_text("process.exit(0);\n")
    (tests / "test_top_level.js").write_text("process.exit(0);\n")
    (tests / "_app_js_loader.mjs").write_text("// shared helper\n")

    # Whole duplicate repo trees left behind by the benchmark rig.
    scratch = tests / "benchmark" / "_runs" / "arm__t0" / "repo" / "tests"
    (scratch / "unit").mkdir(parents=True)
    (scratch / "test_app_hash.mjs").write_text("process.exit(1);\n")
    (scratch / "unit" / "test_dup.mjs").write_text("process.exit(1);\n")
    experiment = tests / "experiments" / "_arm" / "tests"
    experiment.mkdir(parents=True)
    (experiment / "test_dup.js").write_text("process.exit(1);\n")

    found = {_rel_to(tmp_path, p) for p in discover(tmp_path)}
    assert found == {"tests/test_top_level.js", "tests/unit/test_brand_new.mjs"}, (
        f"glob-based discovery returned {sorted(found)}"
    )


# --------------------------------------------------------------------------
# _KNOWN_BROKEN bookkeeping
# --------------------------------------------------------------------------


def test_known_broken_keys_are_a_subset_of_discovered_suites():
    discover, _, known = _helpers()
    discovered = {_rel(p) for p in discover(REPO_ROOT)}
    stale = sorted(set(known) - discovered)
    assert not stale, (
        f"_KNOWN_BROKEN names suites that no longer exist: {stale} "
        "(a renamed or deleted file must not linger in the map)"
    )


def test_known_broken_keys_are_repo_relative_posix_paths():
    _, _, known = _helpers()
    for rel in known:
        assert isinstance(rel, str), f"key must be a str, got {type(rel)!r}"
        assert rel.startswith("tests/"), f"key must be repo-relative: {rel!r}"
        assert "\\" not in rel, f"key must be POSIX-style: {rel!r}"
        assert not rel.startswith("/"), f"key must not be absolute: {rel!r}"


def test_known_broken_reasons_name_the_actual_failure():
    _, _, known = _helpers()
    assert known, "_KNOWN_BROKEN must document the currently-red suites"
    for rel, reason in known.items():
        assert isinstance(reason, str), f"{rel}: reason must be a str"
        assert reason.strip(), f"{rel}: reason must not be empty"
        assert "\n" not in reason, f"{rel}: reason must be one line"

    joined = " ".join(known.values()).lower()
    assert "jsdom" in joined, (
        "the jsdom suites' reason must name the undeclared jsdom dependency"
    )
    assert any(word in joined for word in ("stub", "classlist", "toggle")), (
        "the stale-DOM-stub suites' reason must name that failure"
    )


def test_workspace_wiring_suite_is_green_and_not_excused():
    """This suite must pass and must never be parked in _KNOWN_BROKEN."""
    discover, _, known = _helpers()
    rel = "tests/test_app_workspace_wiring.mjs"
    assert rel in {_rel(p) for p in discover(REPO_ROOT)}
    assert rel not in known, (
        f"{rel} must be green, not excused: {known.get(rel)!r}"
    )


# --------------------------------------------------------------------------
# the gate itself
# --------------------------------------------------------------------------


def test_every_green_suite_exits_zero(suite):
    if suite is None:
        _helpers()  # fails with the missing-helper message
    _, run, _ = _helpers()
    result = run(suite)
    assert result.returncode == 0, (
        f"{_rel(suite)} exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_known_broken_suites_are_skipped_with_evidence(broken_suite):
    """Visible skips carrying the captured output, never silent absence."""
    if broken_suite is None:
        _helpers()  # fails with the missing-helper message
    _, run, known = _helpers()
    rel = broken_suite
    result = run(REPO_ROOT / rel)
    if result.returncode == 0:
        pytest.fail(
            f"{rel} now exits 0 - remove it from _KNOWN_BROKEN "
            f"(reason on file: {known[rel]})"
        )
    pytest.skip(
        f"{rel} is known-broken: {known[rel]}\n"
        f"--- captured output ---\n{result.stdout}{result.stderr}"
    )


def test_known_broken_ratchet_fails_when_a_suite_recovers():
    """Stops the skip list rotting: a recovered suite must be un-listed."""
    discover, run, known = _helpers()
    recovered = {}
    for suite in discover(REPO_ROOT):
        rel = _rel(suite)
        if rel not in known:
            continue
        if run(suite).returncode == 0:
            recovered[rel] = known[rel]
    assert not recovered, (
        "these suites now pass - remove them from _KNOWN_BROKEN: "
        + ", ".join(sorted(recovered))
    )


# --------------------------------------------------------------------------
# _run_suite contract
# --------------------------------------------------------------------------


def test_run_suite_reports_failure_for_a_failing_suite(tmp_path):
    _, run, _ = _helpers()
    failing = tmp_path / "test_deliberately_failing.mjs"
    failing.write_text("process.exit(1);\n", encoding="utf-8")
    result = run(failing)
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode != 0, "a failing suite must report failure"


def test_run_suite_reports_success_for_a_passing_suite(tmp_path):
    _, run, _ = _helpers()
    passing = tmp_path / "test_deliberately_passing.mjs"
    passing.write_text("process.exit(0);\n", encoding="utf-8")
    result = run(passing)
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 0, "a passing suite must report success"


def test_run_suite_does_not_raise_on_failure(tmp_path):
    """check=False: callers embed the captured output instead of catching."""
    _, run, _ = _helpers()
    failing = tmp_path / "test_exit_seven.mjs"
    failing.write_text("process.exit(7);\n", encoding="utf-8")
    result = run(failing)
    assert result.returncode == 7


def test_run_suite_captures_text_output(tmp_path):
    _, run, _ = _helpers()
    noisy = tmp_path / "test_noisy.mjs"
    noisy.write_text(
        "console.log('GATE_MARKER_STDOUT');\nprocess.exit(0);\n", encoding="utf-8"
    )
    result = run(noisy)
    assert isinstance(result.stdout, str), "text=True must yield str output"
    assert "GATE_MARKER_STDOUT" in result.stdout


def test_run_suite_runs_from_the_suite_directory(tmp_path):
    """Each suite must run with cwd set to its own directory."""
    _, run, _ = _helpers()
    (tmp_path / "sibling_marker.txt").write_text("here\n", encoding="utf-8")
    probe = tmp_path / "test_cwd_probe.mjs"
    probe.write_text(
        "import { readFileSync } from 'node:fs';\n"
        "try {\n"
        "  readFileSync('sibling_marker.txt', 'utf8');\n"
        "} catch (err) {\n"
        "  console.error('cwd probe failed: ' + err.message);\n"
        "  process.exit(3);\n"
        "}\n"
        "process.exit(0);\n",
        encoding="utf-8",
    )
    result = run(probe)
    assert result.returncode == 0, (
        f"suite did not run from its own directory: {result.stderr}"
    )


# --------------------------------------------------------------------------
# the gate helpers this module grades
#
# The read-only oracle tests/acceptance_node_suite_gating.py imports these
# three names from this module, so they are the deliverable. They live after
# the tests on purpose: the tests above resolve them through globals() at
# call time, so this file still collects (and reports a precise failure)
# while they are absent.
# --------------------------------------------------------------------------

# Suites red today for reasons outside this story's scope. Keys are
# repo-relative POSIX paths; values are one-line reasons naming the ACTUAL
# failure observed in the run this map was derived from. The ratchet test
# above fails the moment one of these starts passing, so this list cannot
# rot into a permanent excuse.
_KNOWN_BROKEN: dict[str, str] = {
    "tests/test_app_hash.mjs": (
        "jsdom is undeclared and uninstalled: "
        "Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'jsdom'"
    ),
    "tests/test_app_backend_escalated.mjs": (
        "jsdom is undeclared and uninstalled: "
        "Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'jsdom'"
    ),
    "tests/test_app_overview.js": (
        "stale hand-rolled DOM stub: document.body.classList.toggle is not a function"
    ),
    "tests/test_app_search.js": (
        "stale hand-rolled DOM stub: document.body.classList.toggle is not a function"
    ),
    "tests/test_app_js_smoke.js": (
        "stale hand-rolled DOM stub: document.body.classList.toggle is not a function"
    ),
}


def _discover_suites(repo_root):
    """Every runnable Node suite under ``repo_root``/tests, sorted.

    Glob-based, never an explicit file list, so a newly added suite is
    picked up automatically. Any path with a ``_``-prefixed component is
    dropped: that covers ``tests/_app_js_loader.mjs`` (a shared helper, not
    a suite) and the whole duplicate repo trees the benchmark rig leaves
    under ``tests/benchmark/_runs/...`` -- the same rule pytest's addopts
    apply with ``--ignore=tests/benchmark --ignore=tests/experiments``.

    The filter runs on the path RELATIVE to ``repo_root``: an absolute-path
    check would see a ``_``-prefixed worktree directory and silently drop
    every suite, turning this gate into a vacuous green.
    """
    root = Path(repo_root).resolve()
    found = set(root.glob("tests/**/test_*.mjs")) | set(
        root.glob("tests/**/test_*.js")
    )
    return sorted(
        path
        for path in found
        if not any(part.startswith("_") for part in path.relative_to(root).parts)
    )


def _run_suite(path):
    """Run one Node suite from its own directory and capture everything.

    ``check=False`` so a red suite yields a ``CompletedProcess`` with the
    captured output intact instead of raising; ``cwd=path.parent`` because
    each suite resolves its imports relative to its own directory -- and
    because ``os.chdir`` would poison the whole pytest process for every
    test that runs after this one.
    """
    path = Path(path)
    return subprocess.run(
        ["node", path.name],
        cwd=path.parent,
        capture_output=True,
        text=True,
        check=False,
    )
