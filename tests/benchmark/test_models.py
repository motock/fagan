"""Tests for tests/benchmark/models.py per-model temperature/num_ctx overrides."""
from models import _LOCAL_AGENT_ENV, MODELS, _local


def test_devstral_retains_tuned_temperature_and_num_ctx():
    env = MODELS["devstral"]["env"]
    assert env["PIPELINE_LOCAL_TEMPERATURE"] == "0.3"
    assert env["PIPELINE_LOCAL_NUM_CTX"] == "16384"


def test_minimax_entry_unchanged():
    env = MODELS["minimax"]["env"]
    assert env["PIPELINE_LOCAL_TEMPERATURE"] == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_TEMPERATURE"]
    assert env["PIPELINE_LOCAL_NUM_CTX"] == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_NUM_CTX"]
    assert env["PIPELINE_LOCAL_ENDPOINT"] == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_ENDPOINT"]
    assert (
        env["PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS"]
        == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS"]
    )
    assert env["PIPELINE_LOCAL_MAX_STEPS"] == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_MAX_STEPS"]


def test_local_with_overrides_replaces_temperature_and_num_ctx():
    result = _local("some:tag", temperature=1.0, num_ctx=32768)
    assert result["env"]["PIPELINE_LOCAL_TEMPERATURE"] == "1.0"
    assert result["env"]["PIPELINE_LOCAL_NUM_CTX"] == "32768"


def test_local_without_overrides_falls_back_to_shared_defaults():
    result = _local("some:tag")
    assert result["env"]["PIPELINE_LOCAL_TEMPERATURE"] == "0.3"
    assert result["env"]["PIPELINE_LOCAL_NUM_CTX"] == "16384"


def test_gptoss_entry_uses_tuned_temperature_and_num_ctx():
    env = MODELS["gptoss"]["env"]
    assert env["PIPELINE_LOCAL_TEMPERATURE"] == "1.0"
    assert env["PIPELINE_LOCAL_NUM_CTX"] == "32768"
    assert env["PIPELINE_LOCAL_MODEL_DEFAULT"] == "gpt-oss:20b"
    assert env["PIPELINE_BACKEND_DISPATCH"] == "local"


def test_gptoss_temp03_entry_uses_low_temperature_and_matches_gptoss_ctx():
    gptoss_env = MODELS["gptoss"]["env"]
    env = MODELS["gptoss_temp03"]["env"]
    assert env["PIPELINE_LOCAL_TEMPERATURE"] == "0.3"
    assert env["PIPELINE_LOCAL_NUM_CTX"] == gptoss_env["PIPELINE_LOCAL_NUM_CTX"]
    assert env["PIPELINE_LOCAL_NUM_CTX"] == "32768"
    assert env["PIPELINE_LOCAL_MODEL_DEFAULT"] == gptoss_env["PIPELINE_LOCAL_MODEL_DEFAULT"]
    assert env["PIPELINE_BACKEND_DISPATCH"] == "local"
    assert env["PIPELINE_LOCAL_TEMPERATURE"] != gptoss_env["PIPELINE_LOCAL_TEMPERATURE"]


def test_gptoss_devstral_review_matches_gptoss_temp03_dispatch_settings():
    """Same dispatch config as gptoss_temp03 - only the review model/backend
    differ - so this is a controlled asymmetric-review experiment, not a
    confound of two changes at once."""
    baseline = MODELS["gptoss_temp03"]["env"]
    env = MODELS["gptoss_devstral_review"]["env"]
    assert env["PIPELINE_LOCAL_MODEL_DEFAULT"] == baseline["PIPELINE_LOCAL_MODEL_DEFAULT"]
    assert env["PIPELINE_LOCAL_TEMPERATURE"] == baseline["PIPELINE_LOCAL_TEMPERATURE"]
    assert env["PIPELINE_LOCAL_NUM_CTX"] == baseline["PIPELINE_LOCAL_NUM_CTX"]
    assert env["PIPELINE_BACKEND_DISPATCH"] == "local"


def test_gptoss_temp03_auto_matches_gptoss_temp03_except_dispatch_backend():
    """Same dispatch/review settings as gptoss_temp03 - only
    PIPELINE_BACKEND_DISPATCH differs - so this isolates the
    escalate-to-Claude mechanism as the sole variable, not a confound with
    a different temperature/ctx/model."""
    baseline = MODELS["gptoss_temp03"]["env"]
    env = MODELS["gptoss_temp03_auto"]["env"]
    assert env["PIPELINE_LOCAL_MODEL_DEFAULT"] == baseline["PIPELINE_LOCAL_MODEL_DEFAULT"]
    assert env["PIPELINE_LOCAL_TEMPERATURE"] == baseline["PIPELINE_LOCAL_TEMPERATURE"]
    assert env["PIPELINE_LOCAL_NUM_CTX"] == baseline["PIPELINE_LOCAL_NUM_CTX"]
    assert env["PIPELINE_BACKEND_DISPATCH"] == "auto"
    assert baseline["PIPELINE_BACKEND_DISPATCH"] == "local"


def test_gptoss_devstral_review_routes_review_to_devstral_locally():
    env = MODELS["gptoss_devstral_review"]["env"]
    assert env["PIPELINE_BACKEND_REVIEW"] == "local"
    assert env["PIPELINE_LOCAL_REVIEW_MODEL"] == "devstral:24b"
    # Dispatch model must NOT equal the review model - otherwise this isn't
    # actually testing asymmetric review.
    assert env["PIPELINE_LOCAL_MODEL_DEFAULT"] != env["PIPELINE_LOCAL_REVIEW_MODEL"]


def test_qwen36_cell_disables_thinking_and_inherits_shared_defaults():
    """The qwen36 cell runs a Qwen3 hybrid model (batiai/qwen3.6-27b:q3) with
    thinking suppressed via PIPELINE_LOCAL_THINK=false, and otherwise inherits
    the shared local-agent defaults. Guards against accidental removal of the
    think flag (which would silently re-break the tool-calling loop) and
    against drift in the model tag."""
    env = MODELS["qwen36"]["env"]
    assert env["PIPELINE_LOCAL_MODEL_DEFAULT"] == "batiai/qwen3.6-27b:q3"
    assert env["PIPELINE_LOCAL_THINK"] == "false"
    assert env["PIPELINE_BACKEND_DISPATCH"] == "local"
    # Inherits the shared defaults (same Ollama endpoint/ctx/steps as devstral).
    assert env["PIPELINE_LOCAL_TEMPERATURE"] == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_TEMPERATURE"]
    assert env["PIPELINE_LOCAL_NUM_CTX"] == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_NUM_CTX"]
    assert env["PIPELINE_LOCAL_MAX_STEPS"] == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_MAX_STEPS"]
    assert env["PIPELINE_LOCAL_ENDPOINT"] == _LOCAL_AGENT_ENV["PIPELINE_LOCAL_ENDPOINT"]


def test_gptoss_qwen36coder_review_routes_review_to_qwen36coder_locally():
    env = MODELS["gptoss_qwen36coder_review"]["env"]
    assert env["PIPELINE_BACKEND_REVIEW"] == "local"
    assert env["PIPELINE_LOCAL_REVIEW_MODEL"] == "qwen3-coder:30b"
    # Dispatch model must NOT equal the review model - otherwise this isn't
    # actually testing asymmetric review.
    assert env["PIPELINE_LOCAL_MODEL_DEFAULT"] != env["PIPELINE_LOCAL_REVIEW_MODEL"]


def test_gemma4_26b_arm_matches_gptoss_high_except_model_tag():
    gemma = MODELS["gemma4_26b"]["env"]
    gptoss = MODELS["gptoss_high"]["env"]
    differing = {k for k in gemma.keys() | gptoss.keys() if gemma.get(k) != gptoss.get(k)}
    assert differing == {"PIPELINE_LOCAL_MODEL_DEFAULT"}


def test_gemma4_26b_arm_defaults_to_the_hf_gguf_tag(monkeypatch):
    monkeypatch.delenv("BENCH_GEMMA4_TAG", raising=False)
    import importlib

    import models
    reloaded = importlib.reload(models)
    assert reloaded.MODELS["gemma4_26b"]["env"]["PIPELINE_LOCAL_MODEL_DEFAULT"] == (
        "hf.co/google/gemma-4-26B-A4B-it-qat-q4_0-gguf:latest"
    )


def test_gemma4_26b_arm_pins_only_dispatch_to_gemma():
    """The arm is a single-variable comparison: gemma implements, every other
    role stays at the model the gptoss_high baseline resolved for it. Pinning
    review to gemma as well would both confound the comparison and trip the
    RAM gate (gemma's 14909 MB weights cannot be loaded alongside themselves
    on a 24576 MB host at the 0.60 fraction)."""
    rc = MODELS["gemma4_26b"]["role_config"]
    assert set(rc) == {"planner", "dispatch", "test_author", "review", "overlord"}
    assert rc["dispatch"] == {"provider": "ollama", "model": "gemma4-26b-qat"}
    for role in ("planner", "test_author", "review", "overlord"):
        assert rc[role] == {"provider": "ollama", "model": "deepseek-v4.1-flash"}
    # The story's own spec.json pin ("sonnet") must be replaced, or
    # _resolve_dispatch_target hands the driver that literal name.
    assert MODELS["gemma4_26b"]["story_model"] == "gemma4-26b-qat"


def test_qwen36_35b_a3b_temp07_probe_differs_only_in_temperature():
    """The 0.7 probe is a single-variable experiment against the 0.3 arm: same
    model, window, budget, think flag and role pins, so a scorecard difference
    is attributable to the sampling temperature alone."""
    base = MODELS["qwen36_35b_a3b"]
    probe = MODELS["qwen36_35b_a3b_temp07"]
    assert base["env"]["PIPELINE_LOCAL_TEMPERATURE"] == "0.3"
    assert probe["env"]["PIPELINE_LOCAL_TEMPERATURE"] == "0.7"
    differing = {
        k for k in base["env"] | probe["env"]
        if base["env"].get(k) != probe["env"].get(k)
    }
    assert differing == {"PIPELINE_LOCAL_TEMPERATURE"}
    assert probe["role_config"] == base["role_config"]
    assert probe["story_model"] == base["story_model"]


def test_qwen36_35b_a3b_temp07_inherits_the_arms_widened_budget():
    """The probe must stay on the arm's 49152 window and 1800s dispatch
    budget. Falling back to _LOCAL_AGENT_ENV's 16384/900 would silently make
    the temperature probe a confound of window and timeout as well."""
    env = MODELS["qwen36_35b_a3b_temp07"]["env"]
    base = MODELS["qwen36_35b_a3b"]["env"]
    assert env["PIPELINE_LOCAL_NUM_CTX"] == base["PIPELINE_LOCAL_NUM_CTX"] == "49152"
    assert env["PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS"] == "1800"
    assert env["PIPELINE_LOCAL_THINK"] == "false"
    assert env["PIPELINE_BACKEND_DISPATCH"] == "local"


def test_qwen36_35b_a3b_temp09_probe_differs_from_temp07_only_in_temperature():
    """The 0.9 arm extends the temperature sweep one step past the model's own
    recommended 0.7. Same model, window, budget, think flag and role pins, so a
    scorecard difference against the 0.7 arm is attributable to the sampling
    temperature alone -- which is the only thing that makes the 0.3 -> 0.7 -> 0.9
    series readable as a trend rather than three unrelated runs."""
    base = MODELS["qwen36_35b_a3b_temp07"]
    probe = MODELS["qwen36_35b_a3b_temp09"]
    assert base["env"]["PIPELINE_LOCAL_TEMPERATURE"] == "0.7"
    assert probe["env"]["PIPELINE_LOCAL_TEMPERATURE"] == "0.9"
    differing = {
        k for k in base["env"] | probe["env"]
        if base["env"].get(k) != probe["env"].get(k)
    }
    assert differing == {"PIPELINE_LOCAL_TEMPERATURE"}
    assert probe["role_config"] == base["role_config"]
    assert probe["story_model"] == base["story_model"]


def test_qwen36_35b_a3b_temp09_inherits_the_arms_widened_budget():
    """Like the 0.7 probe, the 0.9 arm must stay on the arm's 49152 window and
    1800s dispatch budget. Falling back to _LOCAL_AGENT_ENV's 16384/900 would
    silently make the temperature sweep a confound of window and timeout."""
    env = MODELS["qwen36_35b_a3b_temp09"]["env"]
    base = MODELS["qwen36_35b_a3b_temp07"]["env"]
    assert env["PIPELINE_LOCAL_NUM_CTX"] == base["PIPELINE_LOCAL_NUM_CTX"] == "49152"
    assert env["PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS"] == "1800"
    assert env["PIPELINE_LOCAL_THINK"] == "false"
    assert env["PIPELINE_BACKEND_DISPATCH"] == "local"


def test_qwen36_35b_a3b_temp07_think_high_differs_from_temp07_only_in_think_level():
    """Isolates thinking on the 0.7 arm: same model, temperature, window,
    budget and role pins as qwen36_35b_a3b_temp07, with only the think flag
    moved from `false` to the graded level `high`. Every other qwen36 arm in
    this comparison runs think=false because this model's thinking blocks
    break the tool-calling loop, so this is the arm that measures what that
    suppression was actually buying -- a scorecard difference against temp07
    is attributable to reasoning depth alone, and the two differ in exactly
    one env key."""
    base = MODELS["qwen36_35b_a3b_temp07"]
    probe = MODELS["qwen36_35b_a3b_temp07_think_high"]
    assert base["env"]["PIPELINE_LOCAL_THINK"] == "false"
    assert probe["env"]["PIPELINE_LOCAL_THINK"] == "high"
    differing = {
        k for k in base["env"] | probe["env"]
        if base["env"].get(k) != probe["env"].get(k)
    }
    assert differing == {"PIPELINE_LOCAL_THINK"}
    assert probe["role_config"] == base["role_config"]
    assert probe["story_model"] == base["story_model"]


def test_qwen36_35b_a3b_temp07_think_high_level_survives_think_resolution(monkeypatch):
    """A graded level only reaches /api/chat if `_tuned_think` accepts the
    token. Anything outside true/false/low/medium/high/max falls through to
    None, `LOCAL_AGENT_THINK` is left unset, and the request body omits
    `think` entirely -- which for this hybrid model means its own default
    reasoning depth, not the `high` the arm claims to be testing. Resolving
    the arm's own env value (rather than asserting the raw string) is what
    catches a typo that would otherwise silently run a different experiment."""
    from app.ollama_prompt_utils import _tuned_think

    arm = MODELS["qwen36_35b_a3b_temp07_think_high"]
    monkeypatch.setenv("PIPELINE_LOCAL_THINK", arm["env"]["PIPELINE_LOCAL_THINK"])
    assert _tuned_think(arm["env"]["PIPELINE_LOCAL_MODEL_DEFAULT"]) == "high"


def test_qwen36_35b_a3b_temp07_think_medium_differs_from_temp07_only_in_think_level():
    """The lower-dose sibling of the think_high probe: same model, temperature,
    window, budget and role pins as qwen36_35b_a3b_temp07, with only the think
    flag moved from `false` to the graded level `medium`. high cost three cells
    against temp07 and re-introduced the per-target repetition park that 0.7
    had driven to zero; this arm tests whether the dose-response is monotone
    (any thinking is harmful) or an inverted U with a peak below `high`, which
    is the question a saturated control at 11/12 cannot answer by score
    alone."""
    base = MODELS["qwen36_35b_a3b_temp07"]
    probe = MODELS["qwen36_35b_a3b_temp07_think_medium"]
    assert base["env"]["PIPELINE_LOCAL_THINK"] == "false"
    assert probe["env"]["PIPELINE_LOCAL_THINK"] == "medium"
    differing = {
        k for k in base["env"] | probe["env"]
        if base["env"].get(k) != probe["env"].get(k)
    }
    assert differing == {"PIPELINE_LOCAL_THINK"}
    assert probe["role_config"] == base["role_config"]
    assert probe["story_model"] == base["story_model"]


def test_qwen36_35b_a3b_temp07_think_medium_level_survives_think_resolution(monkeypatch):
    """`medium` is a graded level `_tuned_think` accepts, so it reaches the
    request body as a string rather than being dropped. A token outside
    true/false/low/medium/high/max resolves to None and omits `think`
    entirely, which for this hybrid model means its own default reasoning
    depth -- silently a different experiment from the one the arm claims to
    run, and one the scorecard could not distinguish from the intended one."""
    from app.ollama_prompt_utils import _tuned_think

    arm = MODELS["qwen36_35b_a3b_temp07_think_medium"]
    monkeypatch.setenv("PIPELINE_LOCAL_THINK", arm["env"]["PIPELINE_LOCAL_THINK"])
    assert _tuned_think(arm["env"]["PIPELINE_LOCAL_MODEL_DEFAULT"]) == "medium"


def test_qwen38_27b_arm_runs_at_the_models_recommended_non_thinking_temperature():
    """Qwen3.8-27B is a hybrid thinking model. Its card recommends 0.7 for
    non-thinking mode and 1.0 for thinking mode, so the arm must carry 0.7
    explicitly (the shared default is 0.3, not the recommendation) alongside
    think=false, or it runs at neither the recommended value nor a comparable
    one to the other arms."""
    env = MODELS["qwen38_27b"]["env"]
    assert env["PIPELINE_LOCAL_TEMPERATURE"] == "0.7"
    assert env["PIPELINE_LOCAL_THINK"] == "false"
    assert env["PIPELINE_LOCAL_NUM_CTX"] == "49152"
    assert env["PIPELINE_LOCAL_DISPATCH_TIMEOUT_SECONDS"] == "1800"
    assert env["PIPELINE_BACKEND_DISPATCH"] == "local"


def test_qwen38_27b_arm_differs_from_the_qwen36_probe_only_in_model_tag():
    """Single-variable comparison against qwen36_35b_a3b_temp07: the two arms
    run the same recommended temperature, window, budget, think flag and
    non-dispatch role pins, so a scorecard difference between them is the
    model and nothing else. The role_configs differ ONLY in the dispatch
    model, which is the variable under test -- the reviewer holding both arms
    must stay identical or the comparison is confounded."""
    new = MODELS["qwen38_27b"]
    old = MODELS["qwen36_35b_a3b_temp07"]
    differing = {k for k in new["env"] | old["env"] if new["env"].get(k) != old["env"].get(k)}
    assert differing == {"PIPELINE_LOCAL_MODEL_DEFAULT"}
    assert new["role_config"]["dispatch"] != old["role_config"]["dispatch"]
    for role in ("planner", "test_author", "review", "overlord"):
        assert new["role_config"][role] == old["role_config"][role]


def test_qwen38_27b_arm_pins_only_dispatch_to_qwen38():
    rc = MODELS["qwen38_27b"]["role_config"]
    assert set(rc) == {"planner", "dispatch", "test_author", "review", "overlord"}
    assert rc["dispatch"] == {"provider": "ollama", "model": "qwen38-27b"}
    for role in ("planner", "test_author", "review", "overlord"):
        assert rc[role] == {"provider": "ollama", "model": "deepseek-v4.1-flash"}
    assert MODELS["qwen38_27b"]["story_model"] == "qwen38-27b"


def test_qwen38_27b_arm_defaults_to_the_ud_iq3_xxs_tag(monkeypatch):
    """The arm under test is the Unsloth UD-IQ3_XXS GGUF, not the
    ISTA-DASLab GSQ-RCO IQ3_S the earlier qwen38_wide run used - the two are
    different quants of the same base model and must not be conflated."""
    monkeypatch.delenv("BENCH_QWEN38_27B_TAG", raising=False)
    import importlib

    import models
    reloaded = importlib.reload(models)
    assert reloaded.MODELS["qwen38_27b"]["env"]["PIPELINE_LOCAL_MODEL_DEFAULT"] == (
        "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-IQ3_XXS"
    )
