"""Tests for the multi-ecosystem benchmark harness (cargo + npm).

The pytest path is covered by test_harness_selftest.py and friends; this
file exercises the Gap 3 seams (spec["ecosystem"], setup_workspace,
build_plan, run_groundtruth) for cargo and npm end-to-end. The two new
tasks (lru_cache_rs / interval_merge_js) must load, scaffold a working
build, and grade their groundtruth correctly when the mock backend
writes the reference impl.

Run: pytest tests/benchmark/test_harness_ecosystem.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
PIPELINE_REPO = BENCH.parents[1]
VENV_PY = PIPELINE_REPO / ".venv" / "bin" / "python"
PY = str(VENV_PY) if VENV_PY.exists() else sys.executable

if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

import harness


# ---------- spec.json / load_task ----------
def test_load_task_defaults_ecosystem_to_pytest():
    """Every existing task's spec.json has no `ecosystem` field, so
    load_task must default it to 'pytest' (preserves backwards
    compatibility with all 8 prior tasks)."""
    task = harness.load_task("token_bucket")
    assert task["ecosystem"] == "pytest"
    # And the python source files are still picked up (regression guard
    # for the load_task signature change). Use the import line the
    # acceptance file actually uses (imports the symbol, doesn't define
    # the class).
    assert "TokenBucket" in task["acceptance_source"]
    assert "TokenBucket" in task["groundtruth_source"]


def test_load_task_dispatches_cargo_files_for_cargo_ecosystem():
    """A cargo task must load acceptance.rs and groundtruth.rs (not
    .py), and report ecosystem='cargo' in the loaded dict."""
    task = harness.load_task("lru_cache_rs")
    assert task["ecosystem"] == "cargo"
    # The acceptance file must contain Rust-syntax markers, not Python.
    assert "use bench_task" in task["acceptance_source"]
    assert "#[test]" in task["acceptance_source"]


def test_load_task_dispatches_npm_files_for_npm_ecosystem():
    """A npm task must load acceptance.test.js and groundtruth.test.js,
    and report ecosystem='npm'."""
    task = harness.load_task("interval_merge_js")
    assert task["ecosystem"] == "npm"
    # JS test-runner markers, not Python.
    assert "node:test" in task["acceptance_source"]
    assert "node:assert" in task["acceptance_source"]


def test_load_task_rejects_unknown_ecosystem():
    """An unknown ecosystem in spec.json must fail at load_task time -
    the harness validates upfront, so a typo in a future task is caught
    at `python harness.py --task <name>` rather than at the first
    subprocess call hours into a run."""
    import pytest as _pytest
    bad_spec = BENCH / "tasks" / "lru_cache_rs" / "spec.json"
    original = bad_spec.read_text()
    bad_spec.write_text(original.replace('"cargo"', '"clojure"'))
    try:
        with _pytest.raises(ValueError, match="unknown ecosystem"):
            harness.load_task("lru_cache_rs")
    finally:
        bad_spec.write_text(original)  # restore


# ---------- setup_workspace ----------
def test_setup_workspace_writes_cargo_toml_for_cargo_ecosystem(tmp_path):
    """A cargo-ecosystem workspace must contain Cargo.toml + src/ and
    MUST NOT contain pyproject.toml (which would win the test-runner
    priority in detect_test_command and route cargo tests through
    pytest)."""
    task = harness.load_task("lru_cache_rs")
    cell = tmp_path / "cell"
    paths = harness.setup_workspace(cell, task)
    repo = paths["repo"]
    assert (repo / "Cargo.toml").exists()
    assert (repo / "src" / "lib.rs").exists()
    assert (repo / "target").exists()  # cargo's build dir, in .gitignore
    # pyproject.toml must NOT be written - the agent's work would still
    # merge fine, but detect_test_command would pick pytest and the
    # .rs files would never get a `cargo test` run.
    assert not (repo / "pyproject.toml").exists()
    # The .venv symlink is pytest-specific.
    assert not (repo / ".venv").exists() or not (repo / ".venv").is_symlink()
    # Cargo.toml content sanity.
    cargo_toml = (repo / "Cargo.toml").read_text()
    assert 'edition = "2021"' in cargo_toml
    assert 'name = "bench_task"' in cargo_toml


def test_setup_workspace_writes_package_json_for_npm_ecosystem(tmp_path):
    """An npm-ecosystem workspace must contain package.json with a
    `node --test` test script and src/ + test/ dirs, and must NOT
    contain pyproject.toml."""
    task = harness.load_task("interval_merge_js")
    cell = tmp_path / "cell"
    paths = harness.setup_workspace(cell, task)
    repo = paths["repo"]
    assert (repo / "package.json").exists()
    assert (repo / "src").exists()
    assert (repo / "test").exists()
    assert not (repo / "pyproject.toml").exists()
    pkg = json.loads((repo / "package.json").read_text())
    assert "node --test" in pkg["scripts"]["test"]
    # The test script must glob *.test.js, not just point at the dir -
    # Node 22 treats `node --test test/` as "run the module at test",
    # not "scan the test/ directory".
    assert "*.test.js" in pkg["scripts"]["test"]
    # Node 18+ has node:test as a built-in; document the requirement
    # in the README so operators on older Node don't trip on this.
    # (No runtime check here - we trust the agent's environment.)


def test_setup_workspace_pytest_ecosystem_still_works(tmp_path):
    """Regression: a pytest task (the default) must still write
    pyproject.toml + .venv exactly as before. This is the seam that
    the ecosystem dispatch can NOT break."""
    task = harness.load_task("token_bucket")
    cell = tmp_path / "cell"
    paths = harness.setup_workspace(cell, task)
    repo = paths["repo"]
    assert (repo / "pyproject.toml").exists()
    assert (repo / ".venv").is_symlink()
    # Cargo / npm must NOT have leaked their scaffold files.
    assert not (repo / "Cargo.toml").exists()
    assert not (repo / "package.json").exists()


# ---------- build_plan ----------
def test_build_plan_acceptance_path_dispatches_on_ecosystem():
    """The story's acceptance file path must match the ecosystem:
    pytest -> test_acceptance.py, cargo -> tests/test_acceptance.rs,
    npm -> test/acceptance.test.js. The agent's agent_instructions
    tell it to write the matching tests/X.test.<ext> file, and the
    pipeline materializes the oracle at the right path so the agent's
    work and the oracle line up."""
    import tempfile
    repo = Path(tempfile.mkdtemp())
    py_task = harness.load_task("token_bucket")
    rs_task = harness.load_task("lru_cache_rs")
    js_task = harness.load_task("interval_merge_js")
    py_plan = harness.build_plan(repo, py_task)
    rs_plan = harness.build_plan(repo, rs_task)
    js_plan = harness.build_plan(repo, js_task)
    assert py_plan["epics"][0]["stories"][0]["acceptance"][0]["path"] == "test_acceptance.py"
    assert rs_plan["epics"][0]["stories"][0]["acceptance"][0]["path"] == "tests/test_acceptance.rs"
    assert js_plan["epics"][0]["stories"][0]["acceptance"][0]["path"] == "test/acceptance.test.js"


# ---------- run_groundtruth ----------
def test_run_groundtruth_cargo_runs_cargo_test(tmp_path, monkeypatch):
    """run_groundtruth must invoke `cargo test` (not pytest) when the
    task is cargo-ecosystem, and rc=0 means pass."""
    # Set up a scratch dir with the cargo reference impl + the
    # groundtruth grader. We bypass setup_workspace and write the
    # scratch directly - run_groundtruth's job is to grade, not to
    # scaffold; the input is "an impl file at <scratch>/src/lib.rs and
    # a groundtruth test that uses bench_task".
    scratch = tmp_path / "gt"
    src_dir = scratch / "src"
    tests_dir = scratch / "tests"
    src_dir.mkdir(parents=True)
    tests_dir.mkdir()
    (scratch / "Cargo.toml").write_text(
        '[package]\nname = "bench_task"\nversion = "0.0.0"\nedition = "2021"\n'
        '[lib]\npath = "src/lib.rs"\n'
    )
    # Minimal but correct impl so cargo test has a real lib to compile
    # against (the tests use bench_task::LruCache).
    (src_dir / "lib.rs").write_text(harness._MOCK_IMPLS["lru_cache_rs"].lstrip("\n"))
    # Trivial passing test.
    (tests_dir / "test_g.rs").write_text('''
#[test]
fn it_compiles_and_works() {
    use bench_task::LruCache;
    let mut c = LruCache::new(1);
    c.put(1, 100);
    assert_eq!(c.get(1), Some(100));
}
''')
    # We don't actually want to run cargo here (slow + needs network for
    # the first invocation). Monkeypatch subprocess.run to capture what
    # run_groundtruth would invoke.
    captured = {}
    def _fake_run(argv, **kw):
        captured["argv"] = argv
        captured["cwd"] = kw.get("cwd")
        from unittest.mock import MagicMock
        m = MagicMock()
        m.returncode = 0
        m.stdout = "ok"
        m.stderr = ""
        return m
    monkeypatch.setattr(harness.subprocess, "run", _fake_run)
    # Provide a fake impl_src so the impl-exists check passes.
    impl_src = tmp_path / "impl_src"
    (impl_src / "src").mkdir(parents=True)
    (impl_src / "src/lib.rs").write_text("// impl\n")
    gt = harness.run_groundtruth(
        impl_src, "src/lib.rs", "// gt", scratch, ecosystem="cargo",
    )
    assert captured["argv"][:2] == ["cargo", "test"]
    assert Path(captured["cwd"]) == scratch
    assert gt["ran"] is True
    assert gt["passed"] is True


def test_run_groundtruth_npm_runs_node_test(tmp_path, monkeypatch):
    """run_groundtruth must invoke `node --test` (not pytest) when the
    task is npm-ecosystem, and rc=0 means pass."""
    captured = {}
    def _fake_run(argv, **kw):
        captured["argv"] = argv
        captured["cwd"] = kw.get("cwd")
        from unittest.mock import MagicMock
        m = MagicMock()
        m.returncode = 0
        m.stdout = "ok"
        m.stderr = ""
        return m
    monkeypatch.setattr(harness.subprocess, "run", _fake_run)
    impl_src = tmp_path / "impl_src"
    (impl_src / "src").mkdir(parents=True)
    (impl_src / "src/merge.js").write_text("// impl\n")
    scratch = tmp_path / "gt"
    gt = harness.run_groundtruth(
        impl_src, "src/merge.js", "// gt", scratch, ecosystem="npm",
    )
    assert "node" in captured["argv"][0]
    assert "--test" in captured["argv"]
    assert Path(captured["cwd"]) == scratch
    assert gt["ran"] is True
    assert gt["passed"] is True


def test_run_groundtruth_pytest_still_invocates_pytest(tmp_path, monkeypatch):
    """Regression: pytest-ecosystem run_groundtruth still invokes
    `pytest` exactly as before, with the same scratch layout."""
    captured = {}
    def _fake_run(argv, **kw):
        captured["argv"] = argv
        from unittest.mock import MagicMock
        m = MagicMock()
        m.returncode = 0
        m.stdout = "ok"
        m.stderr = ""
        return m
    monkeypatch.setattr(harness.subprocess, "run", _fake_run)
    impl_src = tmp_path / "impl_src"
    impl_src.mkdir()
    (impl_src / "intervals.py").write_text("# impl\n")
    scratch = tmp_path / "gt"
    harness.run_groundtruth(impl_src, "intervals.py", "# gt", scratch)
    # The existing pytest path uses VENV_PY -m pytest, so argv[0] is
    # the venv python, argv[1] is "-m", argv[2] is "pytest".
    assert captured["argv"][2] == "pytest"


def test_run_groundtruth_reports_failed_cargo_build(tmp_path, monkeypatch):
    """A cargo impl that fails to compile must report ran=True,
    passed=False with the compile error in the tail (NOT a confusing
    'no impl file' reason). This is the same contract pytest uses -
    a model that wrote something that doesn't build looks identical
    to one that wrote something that builds but fails tests, which
    is the correct reading for the benchmark."""
    from unittest.mock import MagicMock
    def _fake_run(argv, **kw):
        m = MagicMock()
        m.returncode = 101
        m.stdout = ""
        m.stderr = "error[E0433]: failed to resolve: use of undeclared crate"
        return m
    monkeypatch.setattr(harness.subprocess, "run", _fake_run)
    impl_src = tmp_path / "impl_src"
    (impl_src / "src").mkdir(parents=True)
    (impl_src / "src/lib.rs").write_text("// impl\n")
    scratch = tmp_path / "gt"
    gt = harness.run_groundtruth(
        impl_src, "src/lib.rs", "// gt", scratch, ecosystem="cargo",
    )
    assert gt["ran"] is True
    assert gt["passed"] is False
    assert "E0433" in gt["tail"]


# ---------- missing acceptance.{ext} (review_story_lock_guard) ----------
def _make_task_dir(tmp_path, name, *, with_acceptance, acceptance_body=None):
    """Scaffold a minimal pytest task dir under tmp_path: spec.json +
    groundtruth.py, and (optionally) acceptance.py. Returns the dir."""
    d = tmp_path / name
    d.mkdir(parents=True)
    (d / "spec.json").write_text(json.dumps({
        "name": name,
        "summary": f"task {name}",
        "impl_file": "impl.py",
        "agent_instructions": "do the thing",
    }))
    (d / "groundtruth.py").write_text(
        "# groundtruth\n"
        f"def test_{name}_ok():\n    assert True\n"
    )
    if with_acceptance:
        (d / "acceptance.py").write_text(acceptance_body if acceptance_body is not None else "# acceptance oracle\n")
    return d


def test_load_task_missing_acceptance_file_does_not_crash(tmp_path, monkeypatch):
    """review_story_lock_guard is the only benchmark task with NO
    acceptance.{ext} file (its groundtruth.py declares acceptance=[]).
    load_task must NOT raise FileNotFoundError on the missing file;
    it must set acceptance_source=None so build_plan can emit an empty
    acceptance list (the supported base-harness bar)."""
    monkeypatch.setattr(harness, "TASKS_DIR", tmp_path)
    _make_task_dir(tmp_path, "no_acceptance_task", with_acceptance=False)
    # (a) does NOT raise.
    task = harness.load_task("no_acceptance_task")
    # (b) acceptance_source is None (not a crash, not "").
    assert task["acceptance_source"] is None
    # groundtruth_source is still read normally.
    assert task["groundtruth_source"] is not None
    assert "def test_" in task["groundtruth_source"]


def test_build_plan_missing_acceptance_emits_empty_list(tmp_path, monkeypatch):
    """When acceptance_source is None, build_plan must emit
    "acceptance": [] (empty list - no fixture), NOT a one-fixture list
    with a None source. This is the supported base-harness bar per
    .claude/rules/pipeline-story-schema.md."""
    import tempfile
    monkeypatch.setattr(harness, "TASKS_DIR", tmp_path)
    _make_task_dir(tmp_path, "no_acceptance_task", with_acceptance=False)
    task = harness.load_task("no_acceptance_task")
    repo = Path(tempfile.mkdtemp())
    plan = harness.build_plan(repo, task)
    story = plan["epics"][0]["stories"][0]
    # Empty list, not a list containing a {path, source} dict.
    assert story["acceptance"] == []
    assert story["acceptance"] is not None


def test_load_task_with_acceptance_file_still_reads_contents(tmp_path, monkeypatch):
    """Regression guard: a task WITH an acceptance.py must still get
    acceptance_source set to the file's contents (the fix must not
    over-broadly nuke the existing fixture path)."""
    monkeypatch.setattr(harness, "TASKS_DIR", tmp_path)
    body = "# THE ORACLE\nassert 1 + 1 == 2\n"
    _make_task_dir(tmp_path, "with_acceptance_task", with_acceptance=True, acceptance_body=body)
    task = harness.load_task("with_acceptance_task")
    assert task["acceptance_source"] == body
    assert task["acceptance_source"] is not None


def test_build_plan_with_acceptance_file_still_emits_one_fixture(tmp_path, monkeypatch):
    """Regression guard: a task WITH an acceptance.py must still get a
    one-fixture acceptance list from build_plan (path + source), not an
    empty list."""
    import tempfile
    monkeypatch.setattr(harness, "TASKS_DIR", tmp_path)
    body = "# THE ORACLE\nassert 1 + 1 == 2\n"
    _make_task_dir(tmp_path, "with_acceptance_task", with_acceptance=True, acceptance_body=body)
    task = harness.load_task("with_acceptance_task")
    repo = Path(tempfile.mkdtemp())
    plan = harness.build_plan(repo, task)
    story = plan["epics"][0]["stories"][0]
    assert len(story["acceptance"]) == 1
    assert story["acceptance"][0]["path"] == "test_acceptance.py"
    assert story["acceptance"][0]["source"] == body


def test_build_plan_preserves_sibling_story_keys(tmp_path, monkeypatch):
    """Regression guard: the acceptance-optional edit must not delete the
    story dict's other keys. A prior local-model str_replace ate
    agent_instructions/persona/model while rewriting the acceptance line,
    and the acceptance-only assertions above stayed green because they
    never checked those keys -- so build_plan silently shipped stories
    with no implementation brief. Assert all sibling keys survive in
    BOTH the with-acceptance and no-acceptance paths."""
    import tempfile
    cases = [
        ("no_acc", False),
        ("with_acc", True),
    ]
    for name, with_acc in cases:
        monkeypatch.setattr(harness, "TASKS_DIR", tmp_path)
        _make_task_dir(
            tmp_path, f"sibling_{name}",
            with_acceptance=with_acc,
            acceptance_body="# oracle\n" if with_acc else None,
        )
        task = harness.load_task(f"sibling_{name}")
        repo = Path(tempfile.mkdtemp())
        story = harness.build_plan(repo, task)["epics"][0]["stories"][0]
        # The three sibling keys a wide str_replace previously deleted:
        assert "agent_instructions" in story, f"agent_instructions missing ({name})"
        assert story["agent_instructions"] == task["agent_instructions"]
        assert "persona" in story, f"persona missing ({name})"
        assert story["persona"] == "software-engineer"
        assert "model" in story, f"model missing ({name})"
        assert story["model"] == "sonnet"
        assert "risk" in story, f"risk missing ({name})"
        assert story["risk"] == "low"


def test_load_task_empty_acceptance_file_reads_as_empty_string(tmp_path, monkeypatch):
    """Boundary: an acceptance.py that EXISTS but is EMPTY must read as
    "" (current behavior preserved), NOT be treated as missing. The
    is_file() guard distinguishes 'absent' from 'present-but-empty'."""
    monkeypatch.setattr(harness, "TASKS_DIR", tmp_path)
    _make_task_dir(tmp_path, "empty_acceptance_task", with_acceptance=True, acceptance_body="")
    task = harness.load_task("empty_acceptance_task")
    assert task["acceptance_source"] == ""
    assert task["acceptance_source"] is not None


def test_real_review_story_lock_guard_task_loads_with_no_acceptance():
    """The actual review_story_lock_guard benchmark task (the one that
    harness_error'd at 0s) must now load without raising and report
    acceptance_source=None, and build_plan must emit acceptance=[]."""
    import tempfile
    task = harness.load_task("review_story_lock_guard")
    assert task["acceptance_source"] is None
    repo = Path(tempfile.mkdtemp())
    plan = harness.build_plan(repo, task)
    story = plan["epics"][0]["stories"][0]
    assert story["acceptance"] == []


# ---------- end-to-end with the mock backend ----------
def _run_mock_ecosystem(task_name, workdir):
    """Run harness.py with --model mock against a non-pytest task.
    The mock backend writes the reference impl for the task (cargo or
    npm), the offline reviewer APPROVEes, the cell merges, and the
    groundtruth grader must pass."""
    env = dict(os.environ)
    for k in ("PLANE_BASE", "PLANE_API_KEY", "PLANE_PROJECT", "PLANE_WORKSPACE"):
        env.pop(k, None)
    subprocess.run(
        [PY, str(BENCH / "harness.py"), "--task", task_name, "--model", "mock",
         "--trial", "0", "--workdir", str(workdir),
         "--timeout", "120", "--tick", "1"],
        check=True, capture_output=True, text=True, env=env,
    )
    cell = Path(workdir) / f"{task_name}__mock__t0"
    return json.loads((cell / "result.json").read_text())


def test_mock_cargo_task_drives_to_done_and_passes_groundtruth(tmp_path):
    """End-to-end: the lru_cache_rs task scaffolds a cargo crate, the
    mock backend writes the Rust reference impl into src/lib.rs, the
    pipeline drives the cell to done, and the cargo groundtruth
    grader (a fresh `cargo test` in a scratch crate) reports the
    impl as correct.

    Skipped if cargo isn't installed locally (it isn't in the
    pipeline venv). The end-to-end "real model" run in Phase 2 is
    what proves the cargo path on a real machine."""
    if subprocess.run(["cargo", "--version"], check=False, capture_output=True).returncode != 0:
        import pytest
        pytest.skip("cargo not installed in this environment")
    r = _run_mock_ecosystem("lru_cache_rs", tmp_path)
    assert r["final_status"] == "done"
    assert r["merged"] is True
    assert r["groundtruth_ran"] is True
    assert r["groundtruth_passed"] is True


def test_mock_npm_task_drives_to_done_and_passes_groundtruth(tmp_path):
    """End-to-end: interval_merge_js scaffolds an npm package, the
    mock backend writes the JS reference impl, the pipeline drives
    the cell to done, and the node --test groundtruth reports the
    impl as correct.

    Skipped if Node 18+ isn't available (we use node:test + node:assert
    which require 18+)."""
    node_version = subprocess.run(
        ["node", "--version"], check=False, capture_output=True, text=True,
    )
    if node_version.returncode != 0:
        import pytest
        pytest.skip("node not installed in this environment")
    # Cheap version check: node --version prints "v18.0.0" etc.
    try:
        major = int(node_version.stdout.lstrip("v").split(".")[0])
    except (ValueError, IndexError):
        import pytest
        pytest.skip(f"could not parse node version: {node_version.stdout!r}")
    if major < 18:
        import pytest
        pytest.skip(f"node {major} < 18 (need 18+ for node:test)")
    r = _run_mock_ecosystem("interval_merge_js", tmp_path)
    assert r["final_status"] == "done"
    assert r["merged"] is True
    assert r["groundtruth_ran"] is True
    assert r["groundtruth_passed"] is True
