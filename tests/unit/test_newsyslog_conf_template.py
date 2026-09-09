"""Tests for launchd/pipeline-logs.newsyslog.conf.template and its rendering
by scripts/generate_launchd_plists.sh.

Story: externalize the newsyslog rotation config the same way the three
launchd plists already are. The committed launchd/pipeline-logs.newsyslog.conf
hardcodes the absolute repo path in its four log-file fields; the template
must carry {{REPO_ROOT}} tokens instead, and the existing generator must
render it with the SAME substitution it already uses for the plists.

Out of scope here (must stay byte-identical, asserted below where cheap):
the committed rendered conf itself, the three *.plist.template files, the
three committed *.plist files, the generator's flag parsing / fail-closed
guard, and the two existing launchd test files.

On a branch without the implementation this file fails at the first test:
launchd/pipeline-logs.newsyslog.conf.template does not exist yet. That is
the expected starting state.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_LAUNCHD = _REPO / "launchd"
_GENERATOR = _REPO / "scripts" / "generate_launchd_plists.sh"
_TEMPLATE = _LAUNCHD / "pipeline-logs.newsyslog.conf.template"
_COMMITTED = _LAUNCHD / "pipeline-logs.newsyslog.conf"

_TOKEN = "{{REPO_ROOT}}"

# The four newsyslog log-file fields that carry the absolute repo path today.
_LOG_NAMES = (
    "advance-scheduler.log",
    "advance-scheduler.err.log",
    "usage-poller.log",
    "usage-poller.err.log",
)


def _committed_repo_root():
    """Absolute repo root encoded in the committed conf's four log paths.

    Derived from the committed artifact rather than from this checkout's
    location, so the byte-for-byte reproduction tests hold no matter where
    the repo is checked out. All four paths must agree on one root.
    """
    text = _COMMITTED.read_text()
    roots = set()
    for name in _LOG_NAMES:
        for match in re.finditer(
            r"^[ \t]*(?P<root>\S+)/" + re.escape(name) + r"\b",
            text,
            re.MULTILINE,
        ):
            roots.add(match.group("root"))
    assert len(roots) == 1, (
        "expected the four committed log paths to share one repo root, "
        f"found: {sorted(roots)}"
    )
    return next(iter(roots))


def _run_generator(args, env, check=True):
    return subprocess.run(
        [str(_GENERATOR), *args],
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


@pytest.fixture
def hermetic_env(tmp_path):
    """Environment with a fake HOME and no MLX_MODEL_PATH leak from the
    caller's shell."""
    env = {k: v for k, v in os.environ.items() if k != "MLX_MODEL_PATH"}
    env["HOME"] = str(tmp_path / "fake-home")
    return env


# ---------- criterion 1: the template exists ----------

def test_newsyslog_template_exists():
    assert _TEMPLATE.is_file(), (
        f"missing {_TEMPLATE} - the newsyslog conf must be templated the "
        "same way as the three *.plist.template files"
    )


def test_committed_rendered_conf_still_tracked():
    # Survivor check: the committed rendered copy is not deleted.
    assert _COMMITTED.is_file(), f"missing {_COMMITTED}"


# ---------- criterion 2: no absolute machine path in the template ----------

def test_template_contains_no_absolute_users_path():
    text = _TEMPLATE.read_text()
    assert "/Users/" not in text, (
        "template still hardcodes an absolute /Users/ machine path - the "
        "whole point of this story is that only {{{{REPO_ROOT}}}} remains"
    )


# ---------- criterion 3: exactly four {{REPO_ROOT}} lines, one per log ----------

def test_template_has_exactly_four_repo_root_tokens_one_per_log_field():
    text = _TEMPLATE.read_text()
    count = text.count(_TOKEN)
    assert count == 4, (
        f"expected exactly 4 {{{{REPO_ROOT}}}} occurrences, found {count}"
    )
    carrying = [ln for ln in text.splitlines() if _TOKEN in ln]
    assert len(carrying) == 4, (
        f"expected exactly 4 lines carrying {{{{REPO_ROOT}}}}, "
        f"found {len(carrying)}"
    )
    for name in _LOG_NAMES:
        tokenized = f"{_TOKEN}/{name}"
        matches = [ln for ln in carrying if tokenized in ln]
        assert len(matches) == 1, (
            f"expected exactly one template line carrying {tokenized!r}, "
            f"found {len(matches)}"
        )


def test_template_is_actually_templated_not_a_bare_copy():
    assert _TEMPLATE.read_bytes() != _COMMITTED.read_bytes(), (
        "template must differ from the committed conf (paths tokenized), "
        "not be a byte-identical copy"
    )


# ---------- the comment block and its relative path survive ----------

def test_template_preserves_comment_block_verbatim():
    committed_comments = [
        ln
        for ln in _COMMITTED.read_bytes().splitlines()
        if ln.lstrip().startswith(b"#")
    ]
    template_comments = [
        ln
        for ln in _TEMPLATE.read_bytes().splitlines()
        if ln.lstrip().startswith(b"#")
    ]
    assert template_comments == committed_comments, (
        "the template's comment block must be byte-identical to the "
        "committed conf's - only the four absolute path prefixes change"
    )


def test_comment_relative_sudo_cp_path_stays_relative_and_untemplated():
    text = _TEMPLATE.read_text()
    comment_lines = [ln for ln in text.splitlines() if "sudo cp" in ln]
    assert comment_lines, (
        "the explanatory comment containing the relative `sudo cp launchd/...`"
        " path must survive templating"
    )
    for ln in comment_lines:
        assert _TOKEN not in ln, (
            "the comment's launchd/... path is RELATIVE - it must not be "
            "rewritten to {{{{REPO_ROOT}}}}"
        )
        assert "launchd/" in ln, "comment's relative launchd/ path was altered"


# ---------- criterion 9: no placeholder token other than {{REPO_ROOT}} ----------

def test_template_uses_no_placeholder_token_other_than_repo_root():
    text = _TEMPLATE.read_text()
    found = set(re.findall(r"\{\{([A-Z_]+)\}\}", text))
    assert found == {"REPO_ROOT"}, (
        f"newsyslog template must use only {{{{REPO_ROOT}}}}; found {found}"
    )
    assert "{{MLX_MODEL_PATH}}" not in text
    assert "{{HOME}}" not in text


# ---------- template fidelity: round-trips to the committed conf ----------

def test_template_round_trips_to_committed_conf_byte_for_byte():
    root = _committed_repo_root().encode()
    rendered = _TEMPLATE.read_bytes().replace(_TOKEN.encode(), root)
    assert rendered == _COMMITTED.read_bytes(), (
        "substituting the real repo root into the template must reproduce "
        "the committed launchd/pipeline-logs.newsyslog.conf exactly - the "
        "template is a token swap, not a reformat"
    )


def test_template_preserves_field_table_alignment_and_trailing_fields():
    root = _committed_repo_root().encode()
    committed_lines = _COMMITTED.read_bytes().splitlines()
    template_lines = _TEMPLATE.read_bytes().splitlines()
    for name in _LOG_NAMES:
        token = f"{_TOKEN}/{name}".encode()
        t_matches = [ln for ln in template_lines if token in ln]
        assert len(t_matches) == 1, f"template line for {name} missing"
        c_token = root + b"/" + name.encode()
        c_matches = [
            ln
            for ln in committed_lines
            if c_token in ln and not ln.lstrip().startswith(b"#")
        ]
        assert len(c_matches) == 1, f"committed line for {name} not found"
        t_tail = t_matches[0].split(token, 1)[1]
        c_tail = c_matches[0].split(c_token, 1)[1]
        assert t_tail == c_tail, (
            f"field-table columns after the {name} path changed - the "
            "whitespace alignment and trailing `644  5  5120  *  NJ` fields "
            "must be preserved byte-for-byte"
        )


# ---------- criterion 6: the extended script is still valid bash ----------

def test_generator_passes_bash_syntax_check():
    proc = subprocess.run(
        ["bash", "-n", str(_GENERATOR)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"bash -n failed:\n{proc.stderr}"


# ---------- criterion 4: fake --repo-root render ----------

def test_generator_renders_newsyslog_conf_for_fake_repo_root(
    tmp_path, hermetic_env
):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    fake_root = "/fake/root"
    _run_generator(
        [
            "--repo-root", fake_root,
            "--out-dir", str(out_dir),
            "--mlx-model-path", "/fake/model/cache",
        ],
        env=hermetic_env,
    )
    rendered = out_dir / "pipeline-logs.newsyslog.conf"
    assert rendered.is_file(), (
        "generator must also render pipeline-logs.newsyslog.conf into "
        "--out-dir (alongside the three plists)"
    )
    text = rendered.read_text()
    for name in _LOG_NAMES:
        assert f"{fake_root}/{name}" in text, (
            f"rendered conf missing {fake_root}/{name}"
        )
    carrying = [ln for ln in text.splitlines() if fake_root + "/" in ln]
    assert len(carrying) == 4, (
        f"expected exactly 4 log-path lines with {fake_root}/ prefix, "
        f"found {len(carrying)}"
    )
    assert _TOKEN not in text, "unresolved {{REPO_ROOT}} token in rendered conf"
    assert "/Users/" not in text
    assert not list(out_dir.glob("*.template")), ".template suffix must be dropped"


def test_newsyslog_render_reuses_the_plist_substitution_mechanism(
    tmp_path, hermetic_env
):
    """Same mechanism, same behavior: a repo root the plist substitution
    already handles (one containing spaces) must render identically here."""
    spaced_root = tmp_path / "fake repo root"
    spaced_root.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _run_generator(
        [
            "--repo-root", str(spaced_root),
            "--out-dir", str(out_dir),
            "--mlx-model-path", "/fake/model/cache",
        ],
        env=hermetic_env,
    )
    text = (out_dir / "pipeline-logs.newsyslog.conf").read_text()
    for name in _LOG_NAMES:
        assert f"{spaced_root}/{name}" in text, (
            f"newsyslog render broke on a repo root containing spaces: "
            f"missing {spaced_root}/{name}"
        )


# ---------- criterion 5: real repo root reproduces the committed conf ----------

def test_generator_render_reproduces_committed_conf_byte_for_byte(
    tmp_path, hermetic_env
):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _run_generator(
        [
            "--repo-root", _committed_repo_root(),
            "--out-dir", str(out_dir),
            "--mlx-model-path", "/fake/model/cache",
        ],
        env=hermetic_env,
    )
    rendered = (out_dir / "pipeline-logs.newsyslog.conf").read_bytes()
    assert rendered == _COMMITTED.read_bytes(), (
        "rendering against the real repo root must reproduce the committed "
        "launchd/pipeline-logs.newsyslog.conf byte-for-byte"
    )


def test_newsyslog_render_is_independent_of_mlx_model_path_value(
    tmp_path, hermetic_env
):
    """The newsyslog render uses only {{REPO_ROOT}}: its output must not
    vary with --mlx-model-path and must never contain its value."""
    out_a = tmp_path / "out-a"
    out_a.mkdir()
    out_b = tmp_path / "out-b"
    out_b.mkdir()
    for out_dir, mlx in (
        (out_a, "/fake/model/cache"),
        (out_b, "/some/other/model.bin"),
    ):
        _run_generator(
            [
                "--repo-root", "/fake/root",
                "--out-dir", str(out_dir),
                "--mlx-model-path", mlx,
            ],
            env=hermetic_env,
        )
        text = (out_dir / "pipeline-logs.newsyslog.conf").read_text()
        assert mlx not in text, (
            "newsyslog conf must not interpolate the mlx model path - it "
            "uses only {{{{REPO_ROOT}}}}"
        )
    assert (
        (out_a / "pipeline-logs.newsyslog.conf").read_bytes()
        == (out_b / "pipeline-logs.newsyslog.conf").read_bytes()
    ), "newsyslog output must not depend on the --mlx-model-path value"


# ---------- criterion 7: the fail-closed guard stays intact ----------

def test_generator_still_fails_closed_without_mlx_model_path(
    tmp_path, hermetic_env
):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    proc = _run_generator(
        ["--repo-root", "/fake/root", "--out-dir", str(out_dir)],
        env=hermetic_env,
        check=False,
    )
    assert proc.returncode != 0, (
        "generator must keep failing closed when no mlx model path is "
        "supplied (flag or env var)"
    )
    assert "--mlx-model-path not given" in proc.stderr, (
        f"pre-existing guard message changed, stderr was: {proc.stderr!r}"
    )
    assert not (out_dir / "pipeline-logs.newsyslog.conf").exists(), (
        "the newsyslog render must not bypass the fail-closed guard"
    )
    assert not list(out_dir.glob("*")), "a failed run must write nothing"


def test_generator_fails_closed_on_empty_mlx_model_path_env_var(
    tmp_path, hermetic_env
):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    env = {**hermetic_env, "MLX_MODEL_PATH": ""}
    proc = _run_generator(
        ["--repo-root", "/fake/root", "--out-dir", str(out_dir)],
        env=env,
        check=False,
    )
    assert proc.returncode != 0, (
        "an empty MLX_MODEL_PATH env var must still trip the guard"
    )
    assert not (out_dir / "pipeline-logs.newsyslog.conf").exists()


# ---------- criterion 8: a tmp --out-dir run touches nothing committed ----------

def test_generator_run_leaves_committed_launchd_files_untouched(
    tmp_path, hermetic_env
):
    before = {
        p: (p.stat().st_mtime_ns, p.read_bytes())
        for p in sorted(_LAUNCHD.iterdir())
        if p.is_file()
    }
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _run_generator(
        [
            "--repo-root", _committed_repo_root(),
            "--out-dir", str(out_dir),
            "--mlx-model-path", "/fake/model/cache",
        ],
        env=hermetic_env,
    )
    assert (out_dir / "pipeline-logs.newsyslog.conf").is_file(), (
        "the run must actually have rendered the newsyslog conf"
    )
    for path, (mtime, content) in before.items():
        assert path.stat().st_mtime_ns == mtime, (
            f"{path.name} mtime changed - the run wrote into launchd/"
        )
        assert path.read_bytes() == content, f"{path.name} content changed"


# ---------- boundary: --out-dir default still applies to the newsyslog ----------

def test_newsyslog_render_honors_out_dir_default(tmp_path, hermetic_env):
    fake_repo = tmp_path / "fake-repo-default-out"
    fake_repo.mkdir()
    _run_generator(
        [
            "--repo-root", str(fake_repo),
            "--mlx-model-path", "/fake/model/cache",
        ],
        env=hermetic_env,
    )
    default_out = fake_repo / "launchd"
    rendered = default_out / "pipeline-logs.newsyslog.conf"
    assert rendered.is_file(), (
        "with --out-dir omitted, the newsyslog conf must land in "
        "<resolved-repo-root>/launchd like the plists do"
    )
    text = rendered.read_text()
    assert f"{fake_repo}/advance-scheduler.log" in text
    assert f"{fake_repo}/usage-poller.err.log" in text


# ---------- the generator drives the render from the template ----------

def test_generator_references_the_newsyslog_template():
    text = _GENERATOR.read_text()
    assert "pipeline-logs.newsyslog.conf.template" in text, (
        "generator must render launchd/pipeline-logs.newsyslog.conf.template "
        "rather than carry a hardcoded copy of the conf"
    )


def test_generator_introduces_no_new_placeholder_tokens():
    text = _GENERATOR.read_text()
    found = set(re.findall(r"\{\{([A-Z_]+)\}\}", text))
    allowed = {"REPO_ROOT", "HOME", "MLX_MODEL_PATH"}
    assert found <= allowed, (
        f"generator references undocumented placeholder token(s): "
        f"{found - allowed}"
    )