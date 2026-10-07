"""Claude-credit guard: an infra failure must never score as a model failure.

FINDINGS.md Run 1: 13 of 15 sonnet cells failed instantly with "out_of_credits"
and were scored as model failures. matrix.py must probe the claude CLI before
each Claude-using cell and, when the probe fails, record an "infra_skipped"
stub without running the cell or writing result.json (so a later --resume
re-runs it) and without aborting the grid; scorecard.py must count such cells
in no denominator.

matrix.py imports its siblings (scorecard, models) by top-level name, so the
bench directory is put on sys.path first -- exactly what harness.py does for
itself at its own import time.
"""
import json
import subprocess
import sys
from pathlib import Path

_BENCH = Path(__file__).resolve().parents[1] / "benchmark"
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))

from tests.benchmark import matrix, models, scorecard

TASK = "token_bucket"
REASON = "out_of_credits"
_ROLES = ("planner", "dispatch", "test_author", "review", "overlord")


def _main(monkeypatch, *argv):
    """Run matrix.main() in process against a synthetic argv."""
    monkeypatch.setattr(sys, "argv", ["matrix.py", *argv])
    return matrix.main()


def _stub(task, model, trial):
    """A completed cell, as harness.py writes it."""
    return {"task": task, "model": model, "trial": trial, "final_status": "done",
            "merged": True, "groundtruth_passed": True, "groundtruth_ran": True,
            "timed_out": False, "elapsed_s": 0.1, "ticks": 1,
            "dispatched_model": model}


def _infra(task, model, trial=0, reason=REASON):
    """The infra_skipped stub the brief specifies for an unavailable Claude."""
    return {"task": task, "model": model, "trial": trial,
            "final_status": "infra_skipped", "merged": False,
            "groundtruth_passed": False, "groundtruth_ran": False,
            "timed_out": False, "elapsed_s": 0, "ticks": 0, "error": reason}


def _assert_infra(result, wd, task, model):
    """result carries every specified stub key/value, and no result.json."""
    for key, value in _infra(task, model, result["trial"]).items():
        assert result[key] == value, (key, result)
    assert not (wd / f"{task}__{model}__t{result['trial']}"
                / "result.json").exists(), "a skipped cell must stay resumable"


def _wire(monkeypatch, runs, verdict):
    """Replace matrix.run_cell/claude_available with recorders."""
    probes = []

    def _probe():
        probes.append(1)
        return verdict()

    def _run_cell(task, model, trial, workdir, timeout, tick):
        runs.append((task, model, trial))
        return _stub(task, model, trial)

    monkeypatch.setattr(matrix, "claude_available", _probe)
    monkeypatch.setattr(matrix, "run_cell", _run_cell)
    return probes


# --- _probe_verdict -------------------------------------------------------

def test_probe_verdict_accepts_a_clean_probe():
    assert matrix._probe_verdict(0, '{"result":"OK","is_error":false}') == (True, "")
    assert matrix._probe_verdict(0, "{}") == (True, "")
    assert matrix._probe_verdict(0, '{"is_error": false}') == (True, "")


def test_probe_verdict_rejects_a_failed_probe():
    for rc, stdout in [
        (1, '{"is_error": false}'),            # nonzero exit
        (0, "claude: command not found\n"),    # not JSON
        (0, ""),                               # empty stdout
        (0, '{"is_error": true}'),              # CLI-reported error
        (0, '[{"is_error": false}]'),          # JSON, but not an object
        (0, '"OK"'),                           # JSON scalar
    ]:
        ok, reason = matrix._probe_verdict(rc, stdout)
        assert ok is False, (rc, stdout)
        assert isinstance(reason, str) and reason, (rc, stdout)


def test_probe_verdict_rejects_credit_and_rate_limit_text():
    for marker in ["out_of_credits", "rate limit", "rate_limit", "usage limit",
                   "Out_Of_Credits", "RATE LIMIT", "Usage Limit Reached"]:
        ok, _ = matrix._probe_verdict(
            0, '{"is_error": false, "result": "failed: ' + marker + '"}')
        assert ok is False, marker
    # Over-broad matching would skip healthy cells: these stay available.
    assert matrix._probe_verdict(0, '{"is_error": false, "result": "OK"}')[0] is True
    assert matrix._probe_verdict(
        0, '{"is_error": false, "result": "credits remain within limits"}')[0] is True


# --- _needs_claude --------------------------------------------------------

def test_needs_claude_is_false_for_mock_entries():
    assert matrix._needs_claude(models.MODELS["mock"]) is False
    # mock-ness outranks the env: a mock entry is never probed.
    assert matrix._needs_claude(
        {"mock": True, "env": {"PIPELINE_BACKEND_DISPATCH": "claude"}}) is False
    assert matrix._needs_claude({"mock": True}) is False


def test_needs_claude_legacy_env_only_entries():
    f = matrix._needs_claude
    assert f({"env": {"PIPELINE_BACKEND_DISPATCH": "claude"}}) is True
    assert f({"env": {"PIPELINE_BACKEND_DISPATCH": "local",
                      "PIPELINE_BACKEND_REVIEW": "claude"}}) is True
    assert f({"env": {"PIPELINE_BACKEND_DISPATCH": "local",
                      "PIPELINE_BACKEND_REVIEW": "local"}}) is False
    # review defaults to claude when the key is absent
    assert f({"env": {"PIPELINE_BACKEND_DISPATCH": "local"}}) is True
    assert f({"env": {}}) is True
    assert f({}) is True               # boundary: no env key at all
    assert f(models.MODELS["gptoss_claude_review"]) is True
    assert f(models.MODELS["gptoss_devstral_review"]) is False


def test_needs_claude_role_config_is_authoritative():
    f = matrix._needs_claude
    ollama = {"provider": "ollama", "model": "glm"}
    claude = {"provider": "claude", "model": "sonnet"}
    all_ollama = {r: ollama for r in _ROLES}
    # every role ollama: the absent review env key must not trigger a probe
    assert f({"env": {"PIPELINE_BACKEND_DISPATCH": "local"},
              "role_config": all_ollama}) is False
    # any single claude role is enough
    for role in _ROLES:
        pins = dict(all_ollama, **{role: claude})
        assert f({"env": {"PIPELINE_BACKEND_DISPATCH": "local"},
                 "role_config": pins}) is True, role
    assert f({"role_config": {r: claude for r in _ROLES}}) is True
    # role_config outranks the env: env says claude, the pins say ollama
    assert f({"env": {"PIPELINE_BACKEND_DISPATCH": "claude"},
              "role_config": all_ollama}) is False


def test_needs_claude_published_arms():
    for name in ("sonnet", "glm_claude_review", "gptoss_claude_review_s60"):
        assert matrix._needs_claude(models.MODELS[name]) is True, name
    assert matrix._needs_claude(models.MODELS["gemma4_26b"]) is False


# --- claude_available -----------------------------------------------------

def test_claude_available_runs_the_documented_probe(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout='{"result": "OK", "is_error": false}', stderr="")

    monkeypatch.setattr(matrix.subprocess, "run", fake_run)
    assert matrix.claude_available() == (True, "")
    cmd, kwargs = calls[0]
    assert cmd == ["claude", "-p", "Reply with the single word OK",
                   "--output-format", "json"]
    assert kwargs["timeout"] == 120
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


def test_claude_available_reports_an_unusable_cli(monkeypatch):
    def missing(cmd, **kwargs):
        raise FileNotFoundError("no claude binary")

    monkeypatch.setattr(matrix.subprocess, "run", missing)
    ok, reason = matrix.claude_available()
    assert ok is False and reason

    def slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(matrix.subprocess, "run", slow)
    ok, reason = matrix.claude_available()
    assert ok is False and reason

    # the verdict is _probe_verdict's: a failing probe is unavailable too
    monkeypatch.setattr(
        matrix.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout='{"is_error": true}', stderr=""))
    ok, reason = matrix.claude_available()
    assert ok is False and reason


# --- main() ---------------------------------------------------------------

def test_main_skips_claude_cells_when_claude_is_unavailable(
        tmp_path, monkeypatch, capsys):
    runs = []
    probes = _wire(monkeypatch, runs, lambda: (False, REASON))
    wd = tmp_path / "wd"
    rc = _main(monkeypatch, "--tasks", TASK, "--models", "sonnet", "--trials", "2",
               "--workdir", str(wd), "--jobs", "1")
    assert rc == 0
    assert runs == []                      # no cell ran
    assert len(probes) == 1                # latched after the first failed probe
    assert capsys.readouterr().err.count(REASON) == 1
    results = json.loads((wd / "results.json").read_text())
    assert sorted(r["trial"] for r in results) == [0, 1]  # grid not aborted
    for r in results:
        _assert_infra(r, wd, TASK, "sonnet")


def test_main_records_a_skip_when_credits_run_out_mid_run(tmp_path, monkeypatch):
    runs = []
    verdicts = [(True, ""), (False, REASON)]
    state = {"n": 0}

    def verdict():
        state["n"] += 1
        return verdicts[min(state["n"] - 1, len(verdicts) - 1)]

    probes = _wire(monkeypatch, runs, verdict)
    wd = tmp_path / "wd"
    rc = _main(monkeypatch, "--tasks", TASK, "--models", "sonnet", "--trials", "2",
               "--workdir", str(wd), "--jobs", "1")
    assert rc == 0
    assert len(probes) == 2                # probed per cell, not once per run
    assert [t[2] for t in runs] == [0]     # only the first cell ran
    results = {r["trial"]: r for r in json.loads((wd / "results.json").read_text())}
    assert results[0]["final_status"] == "done"
    _assert_infra(results[1], wd, TASK, "sonnet")


def test_main_resume_reruns_infra_skipped_cells(tmp_path, monkeypatch):
    runs = []
    box = {"verdict": (False, REASON)}
    probes = _wire(monkeypatch, runs, lambda: box["verdict"])
    wd = tmp_path / "wd"
    argv = ("--tasks", TASK, "--models", "sonnet", "--trials", "2",
            "--workdir", str(wd), "--jobs", "1")
    assert _main(monkeypatch, *argv) == 0
    assert runs == []                       # skipped, never run
    box["verdict"] = (True, "")
    assert _main(monkeypatch, "--resume", *argv) == 0
    assert [t[2] for t in runs] == [0, 1]   # no result.json -> both re-run
    assert len(probes) == 3                 # 1 latched + one per resumed cell


def test_main_resume_returns_existing_results_without_probing(tmp_path, monkeypatch):
    wd = tmp_path / "wd"
    cell = wd / f"{TASK}__sonnet__t0"
    cell.mkdir(parents=True)
    done = _stub(TASK, "sonnet", 0)
    (cell / "result.json").write_text(json.dumps(done))
    runs = []
    probes = _wire(monkeypatch, runs, lambda: (False, REASON))
    rc = _main(monkeypatch, "--tasks", TASK, "--models", "sonnet", "--trials", "1",
               "--workdir", str(wd), "--jobs", "1", "--resume")
    assert rc == 0
    assert runs == []            # nothing to run
    assert probes == []          # the probe sits after the --resume check
    assert json.loads((wd / "results.json").read_text()) == [done]


def test_main_does_not_probe_a_local_review_model(tmp_path, monkeypatch):
    runs = []
    probes = _wire(monkeypatch, runs, lambda: (False, REASON))
    wd = tmp_path / "wd"
    rc = _main(monkeypatch, "--tasks", TASK, "--models", "gptoss_devstral_review",
               "--trials", "2", "--workdir", str(wd), "--jobs", "1")
    assert rc == 0
    assert probes == []                        # no claude role -> never probed
    assert [t[2] for t in runs] == [0, 1]      # the cells ran normally


def test_main_latch_does_not_skip_local_cells(tmp_path, monkeypatch):
    """The latch guards only Claude-using cells; a local arm keeps running."""
    runs = []
    probes = _wire(monkeypatch, runs, lambda: (False, REASON))
    wd = tmp_path / "wd"
    rc = _main(monkeypatch, "--tasks", TASK, "--models", "gptoss_devstral_review",
               "sonnet", "--trials", "1", "--workdir", str(wd), "--jobs", "1")
    assert rc == 0
    assert [t[1] for t in runs] == ["gptoss_devstral_review"]
    assert len(probes) == 1
    results = {r["model"]: r for r in json.loads((wd / "results.json").read_text())}
    assert results["gptoss_devstral_review"]["final_status"] == "done"
    _assert_infra(results["sonnet"], wd, TASK, "sonnet")


def test_main_latch_is_thread_safe(tmp_path, monkeypatch):
    """--jobs > 1 runs _go in threads; the latch must not double-report."""
    import threading

    lock = threading.Lock()
    verdicts = iter([(False, REASON)] * 64)

    def verdict():
        with lock:                       # one probe per cell, serialized
            return next(verdicts)

    runs = []
    probes = _wire(monkeypatch, runs, verdict)
    wd = tmp_path / "wd"
    rc = _main(monkeypatch, "--tasks", TASK, "--models", "sonnet", "--trials", "8",
               "--workdir", str(wd), "--jobs", "4")
    assert rc == 0
    assert runs == []
    # The latch may cut some probes short (racing threads), but every cell is
    # probed at most once and at least one probe happened.
    assert 1 <= len(probes) <= 8
    results = json.loads((wd / "results.json").read_text())
    assert len(results) == 8
    for r in results:
        _assert_infra(r, wd, TASK, "sonnet")


def test_main_still_runs_the_preflight_gate(tmp_path, monkeypatch):
    """The credit guard must not weaken preflight_models (a8b268b)."""
    wd = tmp_path / "wd"
    runs, _ = _wire(monkeypatch, [], lambda: (False, REASON))
    rc = _main(monkeypatch, "--tasks", TASK, "--models", "sonnet", "--trials", "1",
               "--workdir", str(wd), "--jobs", "1")
    assert rc == 0                      # published arms pass the real registry
    assert runs == []                   # skipped, not run
    # a registry that cannot resolve the pins still refuses to run anything
    monkeypatch.setattr(matrix, "preflight_models", lambda ms: ["bad pin"])
    runs2, probes2 = _wire(monkeypatch, [], lambda: (False, REASON))
    rc = _main(monkeypatch, "--tasks", TASK, "--models", "sonnet", "--trials", "1",
               "--workdir", str(wd / "other"), "--jobs", "1")
    assert rc == 2                      # unresolvable pins still refuse to run
    assert runs2 == [] and probes2 == []


def test_main_rejects_unknown_models_before_any_probe(tmp_path, monkeypatch):
    runs = []
    probes = _wire(monkeypatch, runs, lambda: (False, REASON))
    rc = _main(monkeypatch, "--tasks", TASK, "--models", "no_such_model",
               "--trials", "1", "--workdir", str(tmp_path / "wd"), "--jobs", "1")
    assert rc == 2
    assert runs == [] and probes == []


# --- _is_environment_failure ----------------------------------------------

def test_is_environment_failure_ignores_infra_skipped_cells():
    f = matrix._is_environment_failure
    assert not f({"final_status": "infra_skipped"})  # never ran an agent
    assert not f({"final_status": "infra_skipped", "dispatched_model": "sonnet"})
    assert f({"final_status": "harness_error"})
    assert f({"final_status": "parked"})             # no dispatched_model
    assert f({})                                     # boundary: nothing at all
    assert not f({"final_status": "parked", "dispatched_model": "glm"})
    assert not f({"final_status": "done", "dispatched_model": "glm"})


# --- scorecard ------------------------------------------------------------

def test_aggregate_counts_infra_skipped_cells_in_no_denominator():
    done = _stub(TASK, "glm", 0)
    stats = scorecard.aggregate(
        [done, _infra(TASK, "glm", 1), _infra("lru_cache", "sonnet", 0)])
    assert stats == scorecard.aggregate([done])   # no key or count moves
    assert stats[(TASK, "glm")]["trials"] == 1
    assert stats[(TASK, "glm")]["success"] == 1


def test_render_reports_infra_skipped_cells_after_the_model_table():
    done = _stub(TASK, "glm", 0)
    infra = _infra(TASK, "glm", 1)
    line = "Infra-skipped cells (not counted): 1"
    out = scorecard.render([done, infra])
    assert line in out
    assert out.index(line) > out.index("Per-model totals")
    assert "Infra-skipped cells" not in scorecard.render([done])  # none skipped
    assert "Infra-skipped cells (not counted): 2" in scorecard.render(
        [done, infra, _infra("lru_cache", "sonnet", 0)])
    assert line in scorecard.render([infra])  # boundary: nothing else to render