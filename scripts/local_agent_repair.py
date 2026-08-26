"""Syntax/undefined-name repair and detection helpers for scripts/local_agent.py's
run_tool: catching malformed model output (invalid syntax, orphaned names left
behind by a partial edit) before it lands on disk, plus a deterministic
indentation-repair path and the per-edit lint-feedback probe. Split out
purely to keep local_agent.py under the project's line-count target.

_SYNTAX_REJECT_COUNTS is shared mutable state with local_agent.py's run_tool
(which resets a path's count on a successful write) - defined here and
re-imported into local_agent.py so both modules mutate the SAME dict object;
unlike a monkeypatched function, a shared mutable container stays in sync
across the import regardless of which module's name is used to reach it.
"""
import ast
import subprocess
from pathlib import Path

from app import pipeline_mcp_server as p

# Consecutive-rejection counter per path, so a model resubmitting the exact
# same broken content can be escalated instead of silently retrying forever
# (observed: gpt-oss retried near-identical broken content 4x until the
# repetition guard parked the run with no file ever landing). Resets on any
# successful write to that path (see local_agent.py's run_tool). NOT a
# repair mechanism — the write itself is always either exactly what the
# model submitted, or refused; this only tracks how many times in a row
# that refusal happened.
_SYNTAX_REJECT_COUNTS: dict[str, int] = {}


def _python_syntax_error(path_str: str, content: str) -> str | None:
    """Return an ERROR string if `path_str` is a .py file and `content` is not
    valid Python, else None. Defense-in-depth against malformed model output
    (e.g. a stray unified-diff leading '+', or an unmatched triple-quote)
    landing on disk — not an attempt to explain why a model emits it.

    The message quotes the offending line (by e.lineno) plus up to 2 lines of
    context either side, verbatim from the SUBMITTED content — never a
    repaired/transformed version — so the model can see exactly what it wrote
    and where."""
    if not path_str.endswith(".py"):
        return None
    try:
        # compile(), not ast.parse(): ast.parse() only validates grammar
        # (parens balanced, indentation forms a legal block structure) - it
        # does NOT check that `return`/`yield` sit inside a function or
        # `break`/`continue` inside a loop. Those are SyntaxErrors too, but
        # only surface at compile() time. Observed live: a dedented `for`
        # loop landed `return` at module scope, ast.parse() accepted it, and
        # the file reached the groundtruth oracle as an import-breaking
        # SyntaxError this guard exists specifically to catch before disk.
        compile(content, path_str, "exec")
    except SyntaxError as e:
        lines = content.splitlines()
        lineno = e.lineno or 0
        offending = lines[lineno - 1] if 1 <= lineno <= len(lines) else ""
        ctx_start = max(1, lineno - 2)
        ctx_end = min(len(lines), lineno + 2)
        context = "\n".join(f"{i:4d}| {lines[i - 1]}" for i in range(ctx_start, ctx_end + 1))
        return (
            f"ERROR: content for {path_str} has invalid Python syntax at line "
            f"{lineno}: {e}. Offending line: {offending!r}\n"
            f"Context (submitted content, lines {ctx_start}-{ctx_end}):\n{context}\n"
            f"Check for stray formatting artifacts (e.g. a leading '+' from "
            f"pasted diff/patch text, or an unmatched/duplicated triple-quote) "
            f"and retry."
        )
    return None


def _function_name_scopes(tree: ast.AST) -> dict[str, tuple[set[str], set[str]]]:
    """Map each function's name to (assigned_names, loaded_names) within it.

    Shallow and conservative on purpose: every Name node anywhere inside the
    function body (including nested functions/comprehensions) is attributed
    to the outer function rather than modeling real scope nesting, and two
    functions sharing the same name (e.g. same-named methods on different
    classes) collide in the returned dict - a false negative (the check
    silently doesn't fire), never a false positive. Good enough for a
    presence check ("was this name assigned/read anywhere near here"), not a
    real data-flow analysis."""
    scopes: dict[str, tuple[set[str], set[str]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assigned: set[str] = set()
            loaded: set[str] = set()
            for n in ast.walk(node):
                if isinstance(n, ast.Name):
                    if isinstance(n.ctx, ast.Store):
                        assigned.add(n.id)
                    elif isinstance(n.ctx, ast.Load):
                        loaded.add(n.id)
                elif isinstance(n, ast.arg):
                    assigned.add(n.arg)
            scopes[node.name] = (assigned, loaded)
    return scopes


def _newly_undefined_module_defs(old_content: str, new_content: str) -> list[str]:
    """Return names of module-level `def`/`class` statements present in
    `old_content` but deleted by this edit while a reference to that name
    survives anywhere in `new_content` - the shape of the MODE-29 incident
    (2026-07-22): a replace_lines edit deleted only the
    `def _review_story_impl(...):` line itself, leaving its ~300-line body
    correctly indented as trailing dead code inside the CALLER's function
    and the caller's `return _review_story_impl(...)` untouched. That
    result is syntactically valid Python (compile() accepts it - the body
    is now just unreachable code after an earlier return), so only a
    NameError surfaces, at runtime, on every call.

    `_newly_undefined_names` above only tracks function-LOCAL Name-Store/
    Load bindings via `_function_name_scopes` and cannot see this: a `def`
    statement's name isn't an `ast.Name` node, and the deleted function's
    own body being reachable syntax elsewhere is irrelevant to whether the
    NAME `_review_story_impl` is still defined. This is deliberately a
    separate, narrower check (top-level statements only, not nested defs)
    rather than folding module scope into `_function_name_scopes`."""
    try:
        old_tree = ast.parse(old_content)
        new_tree = ast.parse(new_content)
    except SyntaxError:
        return []
    old_top_defs = {
        n.name for n in old_tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    new_top_defs = {
        n.name for n in new_tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    removed = old_top_defs - new_top_defs
    if not removed:
        return []
    new_loaded = {
        n.id for n in ast.walk(new_tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    return [
        f"{name} (module-level def deleted but still called)"
        for name in sorted(removed & new_loaded)
    ]


def _newly_undefined_module_vars(old_content: str, new_content: str) -> list[str]:
    """Return names of module-level VARIABLE assignments (top-level
    ast.Assign / ast.AnnAssign targets) present in `old_content` but deleted by
    this edit while a reference to that name survives anywhere in
    `new_content` - the shape of the MODE-43 incident (2026-07-30,
    TRANSPORT-ALIAS-READERS): a replace_lines edit on
    scripts/local_agent_oracle.py replaced the module-level
    `TIMEOUT = float(os.environ.get("LOCAL_AGENT_TIMEOUT", "900"))` line with a
    duplicate of the preceding `NUM_CTX = ...` line, deleting the `TIMEOUT`
    assignment while every later `TIMEOUT` read survived. compile() accepts the
    result - a missing module-level name is a runtime NameError, not a
    SyntaxError - so only a NameError surfaces, on every call.

    `_newly_undefined_names` only tracks function-LOCAL bindings and
    `_newly_undefined_module_defs` only covers `def`/`class` names - NEITHER
    sees a deleted module-level variable assignment. Deliberately a separate,
    narrower check (top-level statements only) mirroring
    `_newly_undefined_module_defs`."""
    try:
        old_tree = ast.parse(old_content)
        new_tree = ast.parse(new_content)
    except SyntaxError:
        return []

    def _top_assigned_names(tree: ast.AST) -> set[str]:
        names: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    names.update(
                        n.id for n in ast.walk(tgt) if isinstance(n, ast.Name)
                    )
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
        return names

    removed = _top_assigned_names(old_tree) - _top_assigned_names(new_tree)
    if not removed:
        return []
    new_loaded = {
        n.id for n in ast.walk(new_tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    return [
        f"{name} (module-level variable deleted but still read)"
        for name in sorted(removed & new_loaded)
    ]


def _newly_undefined_names(path_str: str, old_content: str, new_content: str) -> list[str]:
    """Return "name (in function)" entries for every name whose only
    assignment within a function existed in `old_content`, was read later in
    that SAME function, and has been deleted by this edit while the read
    survives in `new_content` - the exact shape of two separate live
    incidents (gpt-oss:20b deleting `plan_role_config = _plan_role_config(...)`,
    qwen3-coder:30b deleting `branch = ...`/`worktree = ...`), both of which
    landed a NameError/UnboundLocalError that compile()-based syntax
    checking cannot catch (an undefined name is a runtime error, not a
    SyntaxError). Returns [] (never raises) on a non-.py path or when either
    side fails to parse - a genuine syntax problem is `_python_syntax_error`'s
    job, not this check's."""
    if not path_str.endswith(".py"):
        return []
    try:
        old_scopes = _function_name_scopes(ast.parse(old_content))
        new_scopes = _function_name_scopes(ast.parse(new_content))
    except SyntaxError:
        return []
    orphaned = []
    for name, (old_assigned, old_loaded) in old_scopes.items():
        if name not in new_scopes:
            continue
        new_assigned, new_loaded = new_scopes[name]
        for var in sorted(old_assigned & old_loaded):
            if var in new_loaded and var not in new_assigned:
                orphaned.append(f"{var} (in {name})")
    orphaned.extend(_newly_undefined_module_defs(old_content, new_content))
    orphaned.extend(_newly_undefined_module_vars(old_content, new_content))
    return orphaned


def _var_drop_is_confirmed_loss(
    name: str, old_str: str, new_str: str, orphaned: list[str],
) -> bool:
    """Decide whether a top-level var `name` reported by
    `_dropped_top_level_vars` (which flags any vanished module-level
    assignment unconditionally, with no same-file reference check by
    design) is a genuine unconfirmed loss that should block this
    str_replace, or a legitimate refactor that should pass through.

    Two escapes let it through:
    (a) `new_str` on its own contains a top-level Assign/AnnAssign - this
        edit renamed/replaced the assignment rather than deleting it.
    (b) `name` has no surviving reference anywhere in the new file (i.e.
        it is not in `orphaned`, which already tracks exactly that) AND
        `old_str` itself contains more than the bare assignment (a second
        occurrence of `name`, e.g. a same-edit usage) - the assignment and
        its only use were removed together in this one self-contained
        edit, not left dangling.

    Only when neither escape applies is this a confirmed loss."""
    try:
        new_str_tree = ast.parse(new_str)
    except SyntaxError:
        new_str_tree = None
    if new_str_tree is not None and any(
        isinstance(n, (ast.Assign, ast.AnnAssign)) for n in new_str_tree.body
    ):
        return False
    name_survives = any(entry.split(" (")[0] == name for entry in orphaned)
    return name_survives or old_str.count(name) < 2


def _try_repair_indentation(content: str) -> tuple[str, str] | None:
    """Attempt a deterministic, semantics-preserving indentation repair on
    `content` when compile() rejects it with an IndentationError (unexpected
    indent / unexpected unindent / unindent does not match any outer level).

    The repair ITERATES: re-indent the offending line (e.lineno) to the
    leading whitespace of the nearest preceding non-blank, non-comment line,
    re-compile, and if compile still flags an IndentationError fix the next
    offending line too, until the content compiles clean or a non-indentation
    error (or no progress) is hit. Only when the final content compiles clean
    is (repaired_content, note) returned; otherwise None (fall through to the
    normal rejection path).

    The iteration is required because the decoding defect drops the
    indentation on the `def` line after EVERY decorator in the file, not
    just the first (observed live, 2026-07-17, lru_cache: both the
    `@property` getter `def size` AND the `@size.setter` `def size` were
    dedented to column 0). A single-line repair fixed the getter, but the
    setter still broke compile, so the repair returned None and correct code
    was rejected every retry until the wall-clock park (Mode 21 sibling).

    Rationale (GUIDED_DECOMPOSITION_PLAN.md, 2026-07-16, lru_cache t7/t8/
    t10/t11): the 14B has a reproducible decoding defect that drops the
    leading indentation on the line immediately after a decorator - it
    writes `    @property` then `def size(self):` at column 0, a SyntaxError
    (unexpected unindent) it resubmits byte-identical until it parks. A
    prompt-level worked example did NOT prevent it (t11: the defect is
    decoding-level, not understanding-level). Re-indenting the dedented
    line to match the preceding decorator is exactly what the model
    intended and is whitespace-only, so the groundtruth logic gate still
    catches any real error; this converts a syntax death-loop into
    executable code the test gate can evaluate.

    Scoped to IndentationError only: other SyntaxErrors (return/yield
    outside a function, dangling triple-quote, stray diff '+') are real
    logic/format errors the model must fix, not indentation, and are left
    for the normal rejection path."""
    lines_changed = 0
    last_lineno = None
    for _ in range(64):  # bound: no real file has >64 dedented decorator lines
        try:
            compile(content, "<repair>", "exec")
            break  # clean - done
        except IndentationError as e:
            lineno = e.lineno or 0
        except SyntaxError:
            return None  # non-indentation syntax error - do not touch
        if lineno == last_lineno:
            return None  # re-indent didn't advance past this line - can't fix
        lines = content.splitlines(keepends=True)
        if not (1 <= lineno <= len(lines)):
            return None
        # Find the nearest preceding non-blank, non-comment line to take the
        # target indentation from.
        target = None
        for i in range(lineno - 1, 0, -1):
            prev = lines[i - 1]
            stripped = prev.strip()
            if not stripped or stripped.startswith("#"):
                continue
            target = len(prev) - len(prev.lstrip(" \t"))
            break
        if target is None:
            return None  # no preceding line to reference (e.g. top-level indent)
        cur = lines[lineno - 1]
        cur_stripped = cur.lstrip(" \t")
        cur_indent = len(cur) - len(cur_stripped)
        if cur_indent == target:
            return None  # already at target - re-indenting won't help this line
        lines[lineno - 1] = (" " * target) + cur_stripped
        content = "".join(lines)
        lines_changed += 1
        last_lineno = lineno
    try:
        compile(content, "<repair>", "exec")
    except SyntaxError:
        return None  # exhausted without compiling clean - leave for rejection
    if lines_changed == 0:
        return None  # original was already valid
    note = (f"auto-reindented {lines_changed} dedented line(s) to match the "
            f"preceding line's indentation (decorator-dedent decoding defect)")
    return content, note


def _record_syntax_rejection(path_str: str, err: str, existing_line_count: int | None = None) -> str:
    """Bump the consecutive-rejection counter for `path_str` and, from the
    second consecutive rejection onward, append a nudge to regenerate the
    ENTIRE file from scratch instead of resubmitting the same broken content.
    If *existing_line_count* is provided and exceeds THRESHOLD (500 lines),
    use a different smaller-anchored-edit nudge instead - regenerating a
    large file from scratch risks corrupting the untouched majority of it.
    """
    count = _SYNTAX_REJECT_COUNTS.get(path_str, 0) + 1
    _SYNTAX_REJECT_COUNTS[path_str] = count
    if count >= 2:
        # Threshold for large files: 500 lines. If the file is larger than this,
        # advise a smaller anchored edit instead of regenerating.
        THRESHOLD = 500
        if existing_line_count is not None and existing_line_count > THRESHOLD:
            err += (
                f"\nDo NOT resubmit the same content. The file has {existing_line_count} lines; "
                "instead retry with a SMALLER anchored str_replace: quote a few exact lines of surrounding context immediately before and after the specific span you need to change, and change only that minimal span."
            )
        else:
            err += (
                "\nDo NOT resubmit the same content. Regenerate the ENTIRE file "
                "from scratch, with no diff markers and no surrounding prose."
            )
    return err


def _lint_feedback_for(path_str: str, cwd: Path) -> str:
    """Mode 40: after a successful write to `path_str`, run a fast,
    single-file-scoped lint check and return a short findings suffix to
    append to the tool's success message, or "" when there's nothing to
    report. Only ruff supports cheap single-file scoping (swap the "."
    arg for the file path); other detected linters (eslint, golangci-lint)
    are skipped here and only caught by the full-repo _full_suite_result
    check at done-time, to keep this per-edit check fast.

    The point is closing the loop that let a live incident ship 18 ruff
    violations undetected until CI: the model previously had zero lint
    signal until the very end of a run (or, before this fix, never at
    all locally). This surfaces it at the moment the mistake is made.

    `cwd` is taken as a parameter (not a module-level CWD read) so the
    caller's own current worktree path is always used, including under a
    test's monkeypatch of local_agent.CWD - a module-level import here
    would freeze the value at this module's own load time instead.
    """
    if not path_str.endswith(".py"):
        return ""
    lint = p.detect_lint_command(cwd)
    if lint is None:
        return ""
    lint_dir, cmd = lint
    if not cmd or "ruff" not in cmd[0]:
        return ""
    try:
        res = subprocess.run(  # noqa: PLW1510 (check=False would break test fakes with fixed signatures; see test_local_agent.py)
            [cmd[0], "check", path_str], cwd=lint_dir,
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if res.returncode == 0:
        return ""
    findings = (res.stdout + res.stderr).strip()[:800]
    return f"\n\n[lint] `ruff check {path_str}` found issues (fix before calling done):\n{findings}"


__all__ = [
    "_SYNTAX_REJECT_COUNTS",
    "_function_name_scopes",
    "_lint_feedback_for",
    "_newly_undefined_module_defs",
    "_newly_undefined_module_vars",
    "_newly_undefined_names",
    "_python_syntax_error",
    "_record_syntax_rejection",
    "_try_repair_indentation",
    "_var_drop_is_confirmed_loss",
]
