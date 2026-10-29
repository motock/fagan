"""Real-environment check: the long-lived advance worker pays the import once.

No mock here. This starts ``python -m pipeline.advance_worker`` as a real
subprocess, hands it two inherited pipe descriptors through ``pass_fds``, and
sends three requests for one plan whose manifest is already all-done (so an
advance is a cheap no-op that still walks the real orchestration tick).

Two things only a real run can show:

* the worker's protocol survives real pipe I/O across processes -- one result
  line per request, in order, with stdout/stderr untouched by protocol traffic;
* the second and third answers are far cheaper than the first, because
  ``pipeline.server``'s import (0.9-1.4 s) was paid once at startup rather than
  per plan. The budget below is deliberately loose against the measured
  startup cost and tight against a per-request re-import.

The subprocess is started with ``cwd`` set to this control plane's own source
root and ``PIPELINE_SKIP_ENV_FILE=1`` so the scratch plan store -- not the
operator's ``.pipeline.env`` -- decides where plans live.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import pipeline.server  # noqa: F401
from pipeline.advance_worker import _source_root

PLAN = "worker-real"

# A steady-state answer must be well under this; the one-time import is not.
_WARM_SECONDS = 0.5


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "pipeline test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "pipeline test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One plan whose only story is already ``done``, in a scratch plan store."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("scratch repository\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial commit")

    plans = tmp_path / "plans"
    plans.mkdir()
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()

    manifest = {
        "plan_name": PLAN,
        "repo_root": str(repo),
        "stories": {
            "S-1": {
                "key": "S-1",
                "summary": "already finished",
                "status": "done",
                "dependencies": [],
                "acceptance": [],
                "agent_instructions": "do nothing",
                "persona": "author",
                "model": "gpt-oss:20b",
                "risk": "low",
            }
        },
    }
    (plans / f"{PLAN}.manifest.json").write_text(json.dumps(manifest, indent=2))

    monkeypatch.setenv("PLAN_DIR", str(plans))
    monkeypatch.setenv("WORKTREE_ROOT", str(worktrees))
    monkeypatch.setenv("PIPELINE_SKIP_ENV_FILE", "1")
    monkeypatch.setenv("PIPELINE_AUTONOMY", "gated")
    return plans


def test_worker_answers_repeated_requests_without_repaying_the_import(scratch: Path) -> None:
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    env = {**os.environ, "PLAN_DIR": str(scratch)}

    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-m", "pipeline.advance_worker", str(in_r), str(out_w)],
        cwd=_source_root(),
        env=env,
        pass_fds=(in_r, out_w),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    os.close(in_r)
    os.close(out_w)

    lines: list[dict] = []
    elapsed: list[float] = []
    try:
        with os.fdopen(in_w, "wb", buffering=0) as writer, os.fdopen(
            out_r, "rb", buffering=0
        ) as reader:
            for _ in range(3):
                request_started = time.monotonic()
                writer.write(json.dumps({"plan": PLAN}).encode() + b"\n")
                raw = reader.readline()
                took = time.monotonic() - request_started
                assert raw.endswith(b"\n"), "the worker must answer with one whole line"
                result = json.loads(raw)
                lines.append(result)
                elapsed.append(took)
        # EOF ends the loop; the worker exits 0 and writes no protocol to 1/2.
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=60)

    print(f"worker startup + 3 requests: {time.monotonic() - started:.2f}s")
    for index, (result, took) in enumerate(zip(lines, elapsed, strict=True), start=1):
        print(f"request {index}: {took:.3f}s -> {json.dumps(result)[:200]}")

    assert proc.returncode == 0, stderr.decode()[-2000:]
    assert len(lines) == 3
    for result in lines:
        assert isinstance(result, dict)
        assert result.get("skipped") != "locked"
    assert stdout == b"", "protocol results must never go to stdout"
    assert elapsed[1] < _WARM_SECONDS, f"second answer took {elapsed[1]:.3f}s"
    assert elapsed[2] < _WARM_SECONDS, f"third answer took {elapsed[2]:.3f}s"
