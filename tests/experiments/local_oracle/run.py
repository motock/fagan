"""Reproduce the Fix #1 validation against the PRODUCTION
scripts/local_agent_oracle.py. See plan §Verification step 3.

Drive devstral:24b on the token-bucket task in three arms (thin / rich / thin
+ persona). The harness variant under test is the production script, not the
scratchpad copy. Each arm is judged against the investigator's independent
ground-truth tests in test_groundtruth.py.

Usage: python run.py <A|B|C>
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
assert (REPO / "scripts" / "local_agent_oracle.py").exists(), (
    f"Could not locate scripts/local_agent_oracle.py from {Path(__file__)}; "
    f"computed REPO={REPO}. Move this experiment under the repo if you moved it."
)
EXP = Path(__file__).resolve().parent
PY = str(REPO / ".venv" / "bin" / "python")
ORACLE_AGENT = str(REPO / "scripts" / "local_agent_oracle.py")
GROUNDTRUTH = EXP / "_fixtures" / "groundtruth.py"

API = (
    "Implement a token-bucket rate limiter in a NEW file rate_limiter.py.\n"
    "Provide a class TokenBucket with exactly this interface:\n"
    "  - __init__(self, capacity, refill_rate, now=0.0)\n"
    "  - allow(self, tokens=1.0, now=None) -> bool   # now = current time in "
    "seconds; if None, reuse the bucket's last-known time\n"
)

THIN = API + (
    "\nWrite your own pytest tests in test_rate_limiter.py, run them with "
    "pytest, and make them pass.\n"
)

RICH = API + (
    "\nBehaviour requirements — ALL must hold:\n"
    "  1. The bucket starts FULL (capacity tokens).\n"
    "  2. Tokens refill continuously at refill_rate tokens/second based on the "
    "elapsed time since the last allow() call; the refilled level is CAPPED at "
    "capacity (never more).\n"
    "  3. allow(t, now): first refill up to `now`, then if at least t tokens are "
    "available, deduct t and return True; otherwise deduct NOTHING and return "
    "False.\n"
    "  4. Fractional tokens and fractional seconds must work.\n"
    "  5. Time only moves forward: if `now` is earlier than the last time, treat "
    "the elapsed time as 0 (no refill, no error).\n"
    "  6. allow(t) where t > capacity can never succeed.\n"
    "Write pytest tests in test_rate_limiter.py covering every one of these "
    "rules, run them with pytest, and make them pass.\n"
)

ARMS = {
    "A": {"task": THIN, "persona": False},
    "B": {"task": RICH, "persona": False},
    "C": {"task": THIN, "persona": True},
}


def sh(cmd, cwd, **kw):
    return subprocess.run(cmd, check=False, cwd=cwd, capture_output=True, text=True, **kw)


def persona_body() -> str:
    sys.path.insert(0, str(REPO))
    import pipeline_mcp_server as p
    return p._persona_body("software-engineer")


def main(arm: str) -> None:
    spec = ARMS[arm]
    work = EXP / "arms" / arm
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    sh(["git", "init", "-q"], work)
    sh(["git", "config", "user.email", "exp@local"], work)
    sh(["git", "config", "user.name", "exp"], work)
    (work / "README.md").write_text("# rate limiter experiment (production oracle)\n")
    sh(["git", "add", "-A"], work)
    sh(["git", "commit", "-qm", "init"], work)
    # Acceptance oracle — present BEFORE the loop, read-only to the model.
    shutil.copy(GROUNDTRUTH, work / "test_acceptance.py")

    env = dict(os.environ)
    env.update({
        "LOCAL_AGENT_MODEL": "devstral:24b",
        "LOCAL_AGENT_ENDPOINT": "http://localhost:11434",
        "LOCAL_AGENT_TASK": spec["task"],
        "LOCAL_AGENT_SYSTEM": persona_body() if spec["persona"] else "",
        "LOCAL_AGENT_NUM_CTX": "16384",
        "LOCAL_AGENT_TIMEOUT": "900",
        "LOCAL_AGENT_MAX_STEPS": "30",
        "LOCAL_AGENT_TEMPERATURE": "0.3",
        "LOCAL_AGENT_ACCEPTANCE": json.dumps(["test_acceptance.py"]),
        "LOCAL_AGENT_MODE": "oracle",
    })

    log = work / "agent.log"
    with log.open("w") as lf:
        proc = subprocess.run([PY, ORACLE_AGENT], check=False, cwd=work, env=env,
                              stdout=lf, stderr=subprocess.STDOUT)
    agent_exit = proc.returncode

    result = {"arm": arm, "agent_exit": agent_exit}
    result["has_impl"] = (work / "rate_limiter.py").exists()
    result["oracle_untouched"] = (work / "test_acceptance.py").read_text() == GROUNDTRUTH.read_text()
    result["commits"] = sh(["git", "log", "--oneline"], work).stdout.strip().splitlines()

    # Independent verification: copy groundtruth in again under a different
    # name so the agent's untouchable oracle can't be confused with the
    # investigator's separate judgment file.
    shutil.copy(GROUNDTRUTH, work / "test_groundtruth.py")
    r = sh([PY, "-m", "pytest", "test_groundtruth.py", "-q", "--no-header",
            "-p", "no:cacheprovider"], work)
    result["groundtruth_rc"] = r.returncode
    result["groundtruth_tail"] = (r.stdout + r.stderr)[-800:]

    (work / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({k: v for k, v in result.items()
                      if k != "groundtruth_tail"}, indent=2))
    print("\n--- groundtruth tail ---\n" + result["groundtruth_tail"])


if __name__ == "__main__":
    main(sys.argv[1])
