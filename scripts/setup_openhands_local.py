"""One-time setup for routing dispatch to the local backend (OllamaDriver).

Must be run with the OpenHands CLI's own interpreter, not this project's
venv - OpenHands' env-var override mode (--override-with-envs) only covers
LLM_API_KEY/LLM_BASE_URL/LLM_MODEL, not reasoning_effort or extra_body, so
those are baked into a persisted agent_settings.json instead:

    $(uv tool dir)/openhands/bin/python3 scripts/setup_openhands_local.py

Why this exists (found empirically wiring up Step 4):
- devstral:24b errors on OpenHands' default `reasoning_effort` ("high"),
  which LiteLLM translates into an Ollama `thinking` field devstral rejects.
  Fix: reasoning_effort="none".
- Ollama's default context (131072) made devstral's KV cache exceed 24GB of
  unified memory, forcing a CPU/GPU split that made a single completion time
  out. Fix: pin num_ctx via litellm_extra_body, same value as
  PIPELINE_LOCAL_NUM_CTX (backend.py's OllamaDriver default: 8192).

Re-run this after changing PIPELINE_LOCAL_NUM_CTX - the value is baked into
the persisted file at generation time, not read live.
"""
import os

from openhands.sdk import LLM
from openhands_cli.utils import get_default_cli_agent

PERSISTENCE_DIR = os.path.expanduser(
    os.environ.get("PIPELINE_OPENHANDS_PERSISTENCE_DIR", "~/.claude/openhands-pipeline")
)
NUM_CTX = int(os.environ.get("PIPELINE_LOCAL_NUM_CTX", "8192"))
# Model/base_url/api_key here are placeholders - dispatch always overrides
# them per call via --override-with-envs (LLM_MODEL/LLM_BASE_URL/LLM_API_KEY),
# resolved from PIPELINE_LOCAL_MODEL_* / PIPELINE_LOCAL_ENDPOINT.
PLACEHOLDER_MODEL = "ollama/devstral:24b"
PLACEHOLDER_BASE_URL = "http://localhost:11434"


def main() -> None:
    llm = LLM(
        model=PLACEHOLDER_MODEL,
        api_key="dummy",
        base_url=PLACEHOLDER_BASE_URL,
        reasoning_effort="none",
        litellm_extra_body={"options": {"num_ctx": NUM_CTX}},
        usage_id="agent",
    )
    agent = get_default_cli_agent(llm)
    os.makedirs(PERSISTENCE_DIR, exist_ok=True)
    path = os.path.join(PERSISTENCE_DIR, "agent_settings.json")
    with open(path, "w") as f:
        f.write(agent.model_dump_json())
    print(f"Wrote {path} (reasoning_effort=none, num_ctx={NUM_CTX})")


if __name__ == "__main__":
    main()
