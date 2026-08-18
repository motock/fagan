import subprocess

# Import the module under test
from pipeline import repo_health


# Helper to create a mock subprocess.run result
class MockCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

# Test that no lint signal returns None and subprocess.run is not called

def test_no_lint_signal(monkeypatch):
    monkeypatch.setattr(repo_health, "detect_lint_command", lambda checkout: None)
    called = False
    def fake_run(*args, **kwargs):
        nonlocal called
        called = True
        return MockCompletedProcess()
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = repo_health.lint_baseline_finding("/tmp/repo")
    assert result is None
    assert not called

# Test that a lint run with returncode 0 returns None

def test_lint_success(monkeypatch):
    monkeypatch.setattr(repo_health, "detect_lint_command", lambda checkout: ("/tmp/repo", ["echo", "ok"]))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: MockCompletedProcess(returncode=0))
    assert repo_health.lint_baseline_finding("/tmp/repo") is None

# Test that a lint run with returncode 1 and output contains error

def test_lint_failure_with_error(monkeypatch):
    monkeypatch.setattr(repo_health, "detect_lint_command", lambda checkout: ("/tmp/repo", ["echo", "E501 line too long"]))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: MockCompletedProcess(returncode=1, stdout="", stderr="E501 line too long"))
    result = repo_health.lint_baseline_finding("/tmp/repo")
    assert result is not None
    assert result["kind"] == "lint_baseline_red"
    assert "E501 line too long" in result["detail"]

# Test that output longer than 800 chars is truncated

def test_lint_output_truncation(monkeypatch):
    long_output = "a" * 100000
    monkeypatch.setattr(repo_health, "detect_lint_command", lambda checkout: ("/tmp/repo", ["echo"]))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: MockCompletedProcess(returncode=1, stdout=long_output, stderr=""))
    result = repo_health.lint_baseline_finding("/tmp/repo")
    assert result is not None
    assert len(result["detail"]) == 800

# Test that OSError during subprocess.run returns lint_probe_failed

def test_lint_probe_oserror(monkeypatch):
    monkeypatch.setattr(repo_health, "detect_lint_command", lambda checkout: ("/tmp/repo", ["nonexistent", "cmd"]))
    def fake_run(*args, **kwargs):
        raise OSError("no such binary")
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = repo_health.lint_baseline_finding("/tmp/repo")
    assert result["kind"] == "lint_probe_failed"

# Test that TimeoutExpired during subprocess.run returns lint_probe_failed

def test_lint_probe_timeout(monkeypatch):
    monkeypatch.setattr(repo_health, "detect_lint_command", lambda checkout: ("/tmp/repo", ["sleep", "10"]))
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["sleep", "10"], timeout=1)
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = repo_health.lint_baseline_finding("/tmp/repo")
    assert result["kind"] == "lint_probe_failed"

# Test that a non-positive timeout_s does not escape the never-raise contract.
# subprocess.run raises ValueError for a non-positive timeout (in some Python
# versions), and the module docstring promises every function is fail-safe:
# it must return a finding dict or None and never raise.  The
# except (OSError, subprocess.SubprocessError) clause does not catch
# ValueError, so a non-positive timeout must be guarded up front and converted
# to a lint_probe_failed finding instead of propagating.

def test_lint_probe_nonpositive_timeout(monkeypatch):
    monkeypatch.setattr(repo_health, "detect_lint_command", lambda checkout: ("/tmp/repo", ["echo", "ok"]))
    def fake_run(*args, **kwargs):
        if kwargs.get("timeout", 300) <= 0:
            raise ValueError("timeout must be positive")
        return MockCompletedProcess(returncode=0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = repo_health.lint_baseline_finding("/tmp/repo", timeout_s=0)
    assert result is not None
    assert result["kind"] == "lint_probe_failed"

# Follow-up call after the non-positive timeout: the failed probe must not
# leave any mutated state behind, so a subsequent normal happy-path call still
# delegates to detect_lint_command, runs subprocess.run with a valid timeout,
# and returns the expected finding.

def test_lint_probe_nonpositive_timeout_leaves_no_state(monkeypatch):
    monkeypatch.setattr(repo_health, "detect_lint_command", lambda checkout: ("/tmp/repo", ["echo", "ok"]))
    def fake_run(*args, **kwargs):
        if kwargs.get("timeout", 300) <= 0:
            raise ValueError("timeout must be positive")
        return MockCompletedProcess(returncode=0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    repo_health.lint_baseline_finding("/tmp/repo", timeout_s=0)
    # Normal happy-path call must still work exactly as before.
    assert repo_health.lint_baseline_finding("/tmp/repo", timeout_s=300) is None
