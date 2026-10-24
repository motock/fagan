"""dispatch_story delivers mode=files sources as a snapshot pointer, never inline."""
import json
from pathlib import Path

from pipeline import paths
from pipeline import server as p
from tests.unit.test_dispatch_context import (  # noqa: F401
    _source,
    _story,
    _write,
    ctx_dir,
    env,
    plan_dir,
)


def _files_source(uri):
    return {**_source(uri), "mode": "files"}


def test_should_render_pointer_and_omit_text_for_files_mode(env, plan_dir, ctx_dir):  # noqa: F811
    _, builds = env
    _write(plan_dir, [_files_source(ctx_dir)])

    p.dispatch_story("plan", "CTX-1")

    prompt = builds[0]["prompt"]
    assert "USE-TABS-ALWAYS" not in prompt
    assert "- guide.md" in prompt
    assert str(paths.PLAN_DIR / "context") in prompt


def test_should_materialize_snapshot_files_for_files_mode(env, plan_dir, ctx_dir):  # noqa: F811
    _write(plan_dir, [_files_source(ctx_dir)])

    p.dispatch_story("plan", "CTX-1")

    digest = _story(plan_dir)["context_digests"]["ux"]["digest"].removeprefix("sha256:")
    written = Path(plan_dir) / "context" / digest / "files" / "guide.md"
    assert written.read_text() == "USE-TABS-ALWAYS\n"


def test_should_keep_prompt_mode_inline_alongside_files_mode(env, plan_dir, ctx_dir, tmp_path):  # noqa: F811
    _, builds = env
    other = tmp_path / "other"
    other.mkdir()
    (other / "n.md").write_text("INLINE-NOTE\n")
    _write(plan_dir, [_files_source(ctx_dir), _source(other, name="notes")])

    p.dispatch_story("plan", "CTX-1")

    prompt = builds[0]["prompt"]
    assert "INLINE-NOTE" in prompt and "USE-TABS-ALWAYS" not in prompt
    assert json.loads((plan_dir / "plan.manifest.json").read_text())
