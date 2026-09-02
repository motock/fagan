"""Tests for pipeline.guard_liveness (pure parsing helpers).

The Guard cell of docs/failure_modes.json is free text written by humans.
Two helpers turn it into candidate regression-test file references:

- ``parse_guard_paths(raw)`` -> list[str]: zero or more candidate test-file
  references (plain file NAMES or tests/-relative paths), stripped of
  backticks and trailing parenthetical annotations.
- ``is_no_guard_note(raw)`` -> bool: True when the cell asserts no guard
  exists ("none ..." forms).

These tests use LITERAL strings only - never the live dataset's parse
output. docs/failure_modes.json is not read here at all; every input below
is a verbatim (or verbatim-prefix) copy of a real cell shape observed in
the dataset, embedded as a literal so the parser contract is pinned
independently of the data file's contents.

Parsing stays dumb and literal: a .py token that is clearly not a test
(e.g. "local_agent.py" - no test_ prefix, no tests/ prefix) still yields a
candidate. Existence filtering and test-vs-non-test judgement is the
checker story's job, not the parser's.

Callers pass str only. None is not a valid input; a TypeError on None is
acceptable (and asserted as such below) but the parser must never raise on
any *string* input, however mangled.
"""

from __future__ import annotations

import inspect
import re

import pytest

from pipeline import guard_liveness


# ---------------------------------------------------------------------------
# parse_guard_paths: the observed cell shapes, one table entry per shape.
# Each input is a literal copied from (the prefix of) a real Guard cell.
# ---------------------------------------------------------------------------
PARSE_CASES = [
    # (test id, raw cell, expected candidates)
    pytest.param(
        "none_plain",
        "none identified",
        [],
        id="none-plain-yields-no-candidates",
    ),
    pytest.param(
        "none_pure_config",
        "none (pure config, no code)",
        [],
        id="none-pure-config-parenthetical",
    ),
    pytest.param(
        "none_operational",
        "none (operational)",
        [],
        id="none-operational-parenthetical",
    ),
    pytest.param(
        "none_gap_not_guard",
        "none (gap, not a guard)",
        [],
        id="none-gap-not-a-guard-parenthetical",
    ),
    pytest.param(
        "none_direct_repair",
        "none (direct repair, no new test)",
        [],
        id="none-direct-repair-parenthetical",
    ),
    pytest.param(
        "single_file_with_symbol_annotation",
        "`test_backend.py` (`test_chat_retries_transient_co...`)",
        ["test_backend.py"],
        id="single-file-parenthetical-symbol-stripped",
    ),
    pytest.param(
        "two_files_plus_joined",
        "`test_local_agent.py` + `test_local_agent_oracle.py`",
        ["test_local_agent.py", "test_local_agent_oracle.py"],
        id="two-files-plus-joined",
    ),
    pytest.param(
        "two_files_plus_joined_truncated",
        "`test_acceptance_oracle_gate_prior_gate.py` + `tes...",
        ["test_acceptance_oracle_gate_prior_gate.py"],
        id="two-files-plus-joined-truncated-second-token",
    ),
    pytest.param(
        "bare_path_tests_unit_prefix",
        "tests/unit/test_test_author_park_only_and_optout.py",
        ["tests/unit/test_test_author_park_only_and_optout.py"],
        id="bare-tests-unit-path-kept-verbatim",
    ),
    pytest.param(
        "file_with_constant_symbol",
        "`test_pipeline_mcp_server.py` (`DISPATCH_STARTUP_GRACE_SECONDS`)",
        ["test_pipeline_mcp_server.py"],
        id="file-parenthetical-constant-stripped",
    ),
    pytest.param(
        "non_test_citation_requirements",
        "`requirements-dev.txt` exact pin (not a test - mac...)",
        [],
        id="non-py-citation-yields-no-candidate",
    ),
    pytest.param(
        "none_ci_config_names_non_test_file",
        "none (CI config change, `.github/workflows/ci.yml`...)",
        [],
        id="none-cell-naming-non-test-file-yields-nothing",
    ),
    pytest.param(
        "file_with_prose_note",
        "`test_conftest_env_isolation.py` (added later, 202...",
        ["test_conftest_env_isolation.py"],
        id="file-parenthetical-prose-note-stripped",
    ),
]


class TestParseGuardPathsObservedShapes:
    """Every observed Guard-cell shape from the dataset, as literals."""

    @pytest.mark.parametrize("case", PARSE_CASES, ids=[c.id for c in PARSE_CASES])
    def test_shape(self, case):
        raw, expected = case.values
        assert guard_liveness.parse_guard_paths(raw) == expected


class TestParseGuardPathsRules:
    """The lettered rules (a)-(f) from the module contract."""

    def test_rule_a_none_is_case_insensitive(self):
        assert guard_liveness.parse_guard_paths("None identified") == []
        assert guard_liveness.parse_guard_paths("NONE (operational)") == []
        assert guard_liveness.parse_guard_paths("  none  ") == []

    def test_rule_a_none_only_checks_first_token(self):
        # "none" must be the first word/token; a cell that merely *contains*
        # the word none later is a guard citation, not a no-guard note.
        raw = "`test_backend.py` (none of the above)"
        assert guard_liveness.parse_guard_paths(raw) == ["test_backend.py"]

    def test_rule_b_backticked_tokens_extracted(self):
        assert guard_liveness.parse_guard_paths("`test_a.py`") == ["test_a.py"]

    def test_rule_b_bare_py_filename_outside_backticks(self):
        # mode 51's form: a bare path with a tests/unit/ prefix, no backticks.
        raw = "tests/unit/test_test_author_park_only_and_optout.py"
        assert guard_liveness.parse_guard_paths(raw) == [
            "tests/unit/test_test_author_park_only_and_optout.py"
        ]

    def test_rule_b_bare_py_filename_plain_name(self):
        assert guard_liveness.parse_guard_paths("test_a.py") == ["test_a.py"]

    def test_rule_c_non_py_token_yields_no_candidate(self):
        assert guard_liveness.parse_guard_paths("`requirements-dev.txt`") == []
        assert guard_liveness.parse_guard_paths("`.github/workflows/ci.yml`") == []
        assert guard_liveness.parse_guard_paths("`README.md`") == []

    def test_rule_c_non_py_token_among_py_tokens_dropped(self):
        raw = "`test_a.py` + `requirements-dev.txt`"
        assert guard_liveness.parse_guard_paths(raw) == ["test_a.py"]

    def test_rule_d_plus_separated_tokens_each_yield(self):
        raw = "`test_a.py` + `test_b.py`"
        assert guard_liveness.parse_guard_paths(raw) == ["test_a.py", "test_b.py"]

    def test_rule_d_comma_separated_tokens_each_yield(self):
        raw = "`test_a.py`, `test_b.py`"
        assert guard_liveness.parse_guard_paths(raw) == ["test_a.py", "test_b.py"]

    def test_rule_e_trailing_parenthetical_directly_after_filename_stripped(self):
        raw = "`test_pipeline_mcp_server.py` (`DISPATCH_STARTUP_GRACE_SECONDS`)"
        assert guard_liveness.parse_guard_paths(raw) == ["test_pipeline_mcp_server.py"]

    def test_rule_e_parenthetical_content_is_dropped_not_returned(self):
        # The annotation must never leak into the candidate list, even when
        # it itself contains backticked tokens.
        raw = "`test_x.py` (`a`, `b`)"
        assert guard_liveness.parse_guard_paths(raw) == ["test_x.py"]

    def test_rule_e_parenthetical_not_directly_after_filename_is_not_stripped(self):
        # Prose between the filename and the parenthesis: the parenthetical
        # is not glued to the token, so the token itself is unaffected.
        raw = "`test_a.py` see note (why: because)"
        assert guard_liveness.parse_guard_paths(raw) == ["test_a.py"]

    def test_rule_f_dedupe_preserves_order(self):
        raw = "`test_b.py` + `test_a.py` + `test_b.py`"
        assert guard_liveness.parse_guard_paths(raw) == ["test_b.py", "test_a.py"]

    def test_rule_f_dedupe_across_bare_and_backticked_forms(self):
        raw = "`test_a.py` + test_a.py"
        assert guard_liveness.parse_guard_paths(raw) == ["test_a.py"]


class TestParseGuardPathsBoundary:
    """Empty / whitespace / degenerate inputs - never raise, return []."""

    def test_empty_string(self):
        assert guard_liveness.parse_guard_paths("") == []

    def test_whitespace_only(self):
        assert guard_liveness.parse_guard_paths("   ") == []
        assert guard_liveness.parse_guard_paths("\n\t  \n") == []

    def test_none_raises_typeerror(self):
        # Documented contract: callers pass str only. A TypeError on None is
        # acceptable; anything else (e.g. returning [] silently, or a
        # non-TypeError exception) is not.
        with pytest.raises(TypeError):
            guard_liveness.parse_guard_paths(None)  # type: ignore[arg-type]

    def test_no_backticks_no_py_no_candidates(self):
        assert guard_liveness.parse_guard_paths("see the review thread") == []

    def test_trailing_punctuation_after_filename(self):
        # A filename at the end of a sentence must not keep the period.
        assert guard_liveness.parse_guard_paths("added `test_a.py`.") == ["test_a.py"]

    def test_surrounding_whitespace_stripped_from_token(self):
        assert guard_liveness.parse_guard_paths("  `test_a.py`  ") == ["test_a.py"]

    def test_py_token_without_test_prefix_still_yields(self):
        # Parsing stays dumb and literal: "local_agent.py" is not obviously a
        # test (no test_ prefix, no tests/ prefix) but it IS a .py filename,
        # so it yields a candidate. Existence/test-ness filtering is the
        # checker's job.
        assert guard_liveness.parse_guard_paths("`local_agent.py`") == ["local_agent.py"]

    def test_result_is_a_list_of_str(self):
        result = guard_liveness.parse_guard_paths("`test_a.py` + `test_b.py`")
        assert isinstance(result, list)
        assert all(isinstance(p, str) for p in result)

    def test_no_subprocess_or_io_in_result_semantics(self):
        # Pure function: same input, same output, no state.
        raw = "`test_a.py` + `test_b.py`"
        first = guard_liveness.parse_guard_paths(raw)
        second = guard_liveness.parse_guard_paths(raw)
        assert first == second
        assert first is not second  # no shared mutable module-level list


class TestIsNoGuardNote:
    """is_no_guard_note: True iff the cell asserts no guard exists."""

    @pytest.mark.parametrize(
        "raw",
        [
            "none identified",
            "none (pure config, no code)",
            "none (operational)",
            "none (gap, not a guard)",
            "none (direct repair, no new test)",
            "none (CI config change, `.github/workflows/ci.yml`...)",
            "None identified",
            "NONE",
            "  none  ",
        ],
        ids=[
            "plain", "pure-config", "operational", "gap", "direct-repair",
            "ci-config", "capitalized", "uppercase", "whitespace",
        ],
    )
    def test_none_forms_are_true(self, raw):
        assert guard_liveness.is_no_guard_note(raw) is True

    @pytest.mark.parametrize(
        "raw",
        [
            "`test_backend.py` (`test_chat_retries_transient_co...`)",
            "`test_local_agent.py` + `test_local_agent_oracle.py`",
            "tests/unit/test_test_author_park_only_and_optout.py",
            "`requirements-dev.txt` exact pin (not a test - mac...)",
            "`test_conftest_env_isolation.py` (added later, 202...",
            "",
            "   ",
            "no guard was added",
        ],
        ids=[
            "single-file", "two-files", "bare-path", "requirements",
            "prose-note", "empty", "whitespace", "prose-without-none",
        ],
    )
    def test_guard_citations_and_degenerate_inputs_are_false(self, raw):
        assert guard_liveness.is_no_guard_note(raw) is False

    def test_none_later_in_cell_is_not_a_no_guard_note(self):
        # Only the FIRST token decides; a citation that merely mentions
        # "none" in prose is a guard citation.
        assert guard_liveness.is_no_guard_note("`test_a.py` (none found)") is False

    def test_returns_real_bool(self):
        assert isinstance(guard_liveness.is_no_guard_note("none"), bool)
        assert isinstance(guard_liveness.is_no_guard_note("`test_a.py`"), bool)


class TestModuleContract:
    """Structural requirements from the story brief."""

    def _module_ast(self):
        return ast.parse(inspect.getsource(guard_liveness))

    def test_public_surface_is_exactly_two_functions(self):
        # "Public surface to implement (2 functions, no more)." Private
        # helpers (leading underscore) and non-function module attributes
        # (constants, __all__) are allowed; public functions are not.
        public = [
            name
            for name, obj in vars(guard_liveness).items()
            if not name.startswith("_")
            and getattr(obj, "__module__", None) == guard_liveness.__name__
            and inspect.isfunction(obj)
        ]
        assert sorted(public) == ["is_no_guard_note", "parse_guard_paths"]

    def test_functions_are_callable(self):
        assert callable(guard_liveness.parse_guard_paths)
        assert callable(guard_liveness.is_no_guard_note)

    def test_signatures_match_the_brief(self):
        # parse_guard_paths(raw: str) -> list[str]
        psig = inspect.signature(guard_liveness.parse_guard_paths)
        assert list(psig.parameters) == ["raw"]
        # Accept both live annotations (str) and PEP 563 string forms
        # ("str"), depending on the module's __future__ import.
        assert psig.parameters["raw"].annotation in (str, "str")
        assert psig.return_annotation in (list[str], "list[str]")
        # is_no_guard_note(raw: str) -> bool
        isig = inspect.signature(guard_liveness.is_no_guard_note)
        assert list(isig.parameters) == ["raw"]
        assert isig.parameters["raw"].annotation in (str, "str")
        assert isig.return_annotation in (bool, "bool")

    def test_no_imports_beyond_stdlib(self):
        # "contains no imports beyond stdlib" - checked on the AST so a
        # docstring that merely mentions a module name cannot trip it.
        imported = set()
        for node in ast.walk(self._module_ast()):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    imported.add("<relative-import>")
                elif node.module:
                    imported.add(node.module.split(".")[0])
        non_stdlib = imported - set(sys.stdlib_module_names)
        assert not non_stdlib, f"non-stdlib imports: {sorted(non_stdlib)}"

    def test_no_io_or_subprocess_anywhere_in_source(self):
        # "No subprocesses, no filesystem access, no I/O of any kind" -
        # AST-level: no I/O-capable imports, no I/O-shaped calls. Checked
        # on the AST so prose in the docstring cannot trip it.
        banned_imports = {
            "subprocess", "os", "pathlib", "shutil", "socket", "urllib",
            "io", "glob", "tempfile", "ftplib", "http", "requests",
        }
        banned_name_calls = {"open", "print", "input", "eval", "exec", "__import__"}
        banned_attr_calls = {
            "open", "read", "write", "read_text", "write_text",
            "read_bytes", "write_bytes", "mkdir", "remove", "unlink",
            "rename", "rmdir", "popen", "system", "Popen", "check_output",
        }
        for node in ast.walk(self._module_ast()):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in banned_imports, f"imports I/O-capable {root}"
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                assert root not in banned_imports, f"imports from I/O-capable {root}"
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    bad = node.func.id
                    assert bad not in banned_name_calls, f"calls {bad}()"
                elif isinstance(node.func, ast.Attribute):
                    bad = node.func.attr
                    assert bad not in banned_attr_calls, f"calls .{bad}()"

    def test_no_module_level_side_effects(self):
        # Importing the module in a fresh interpreter - with builtins.open
        # spied BEFORE the import - must open nothing, and both helpers
        # must work. (This test itself uses I/O; the module must not.)
        repo_root = Path(__file__).resolve().parents[2]
        code = (
            "import builtins\n"
            "opened = []\n"
            "real_open = builtins.open\n"
            "def _spy(*a, **k):\n"
            "    opened.append(a[0] if a else k)\n"
            "    return real_open(*a, **k)\n"
            "builtins.open = _spy\n"
            "import pipeline.guard_liveness as g\n"
            "assert g.parse_guard_paths('none identified') == []\n"
            "assert g.parse_guard_paths('`test_a.py` + `test_b.py`') == "
            "['test_a.py', 'test_b.py']\n"
            "print('OPENED', opened)\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(repo_root)
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=60,
            cwd=str(repo_root), env=env, check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "OPENED []" in proc.stdout, proc.stdout
