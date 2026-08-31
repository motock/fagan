"""Tool-dispatch impls for the oracle agent, split out of local_agent_oracle.py.

LAO-TOOLS: run_tool and safe_run_tool moved here verbatim from
scripts/local_agent_oracle.py (twin of the LA-TOOLS recipe). Each impl takes an
``origin`` dict — the CALLER's module globals, built fresh inside lao's
delegating wrapper on every call — so:

  - tests that monkeypatch an attribute on scripts.local_agent_oracle
    (ACCEPTANCE_PATHS, the state sets, run_tool itself, ...) are honored,
    because the wrapper reads its own module globals at call time;
  - shared state (``_CREATED_THIS_RUN``, ``_VIEWED_THIS_RUN``,
    ``_SYNTAX_REJECT_COUNTS``) is mutated IN PLACE via origin, so the set/dict
    objects lao owns are the ones updated — never rebound.

This module must NOT import scripts.local_agent_oracle (that would pin the
twin in sys.modules and defeat lao's env-freshness eviction). Only stdlib
imports the bodies actually use live here; everything else routes via origin.
"""
from __future__ import annotations

import shlex
import subprocess


def run_tool_impl(origin, fn, args) -> str:
    if fn in ("create_file", "str_replace") and origin["is_oracle_path"](args.get("path", "")):
        return (f"ERROR: {args['path']} is the read-only acceptance suite and "
                f"must NOT be modified. Change the implementation file instead.")
    if fn == "create_file":
        path = origin["CWD"] / args["path"]
        preexisting = path.exists() and path.read_text().strip()
        if (preexisting
                and args["path"] not in origin["_CREATED_THIS_RUN"]
                and args["path"] not in origin["_VIEWED_THIS_RUN"]):
            return (
                f"ERROR: {args['path']} already exists and is non-empty. Use "
                f"view_file to read it first, then create_file to overwrite it "
                f"with the full corrected contents."
            )
        content = args.get("content", "")
        err = origin["_python_syntax_error"](args["path"], content)
        note = None
        if err:
            repair = origin["_try_repair_indentation"](content)
            if repair is None:
                return origin["_record_syntax_rejection"](args["path"], err)
            content, note = repair
        if preexisting:
            dropped = origin["_dropped_top_level_defs"](path.read_text(), content)
            if path.suffix == ".py":
                dropped += origin["_dropped_top_level_vars"](path.read_text(), content)
            if dropped and not args.get("confirm_removals"):
                return (
                    f"ERROR: this create_file overwrite of {args['path']} would "
                    f"silently drop {len(dropped)} top-level def/class that exist "
                    f"in the current file but not in your new content: "
                    f"{', '.join(dropped)}. If this is unintentional, view_file "
                    f"the current contents and include these definitions in your "
                    f"rewrite (use str_replace/replace_lines for a small targeted "
                    f"change instead of a full rewrite). If the removal is "
                    f"intentional, repeat this exact call with "
                    f"confirm_removals=true. The file was NOT overwritten."
                )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        origin["_SYNTAX_REJECT_COUNTS"].pop(args["path"], None)
        origin["_CREATED_THIS_RUN"].add(args["path"])
        return (f"created {args['path']}" + (f" ({note})" if note else "")
                + origin["_lint_feedback_for"](args['path'], origin["CWD"]))
    if fn == "str_replace":
        path = origin["CWD"] / args["path"]
        if not path.exists():
            return f"ERROR: {args['path']} does not exist (use create_file for new files)."
        text = path.read_text()
        n = text.count(args["old_str"])
        if n == 0:
            return origin["_str_replace_not_found_diag"](args["path"], text, args["old_str"])
        if n > 1:
            return f"ERROR: old_str occurs {n} times in {args['path']}; include more context to make it unique."
        new_text = text.replace(args["old_str"], args["new_str"])
        err = origin["_python_syntax_error"](args["path"], new_text)
        note = None
        if err:
            repair = origin["_try_repair_indentation"](new_text)
            if repair is None:
                return origin["_record_syntax_rejection"](args["path"], err, len(text.splitlines()))
            new_text, note = repair
        orphaned = origin["_newly_undefined_names"](args["path"], text, new_text)
        if orphaned:
            return (
                f"ERROR: this edit to {args['path']} deletes the only assignment "
                f"to {', '.join(orphaned)} while a use of it survives elsewhere - "
                f"this will raise NameError/UnboundLocalError at runtime. Keep the "
                f"assignment, remove the surviving use too, or replace it with an "
                f"equivalent. The edit was NOT applied."
            )
        # Top-level-symbol-loss check: name any def/class that this edit
        # removes in its entirety, even when no same-file reference survives
        # (the symbol may be consumed by OTHER files) - this half is
        # unconditional. A dropped top-level var/constant is only added when
        # it is a genuine unconfirmed loss per _var_drop_is_confirmed_loss:
        # a rename (new_str itself assigns a top-level name) or a
        # self-contained removal (the assignment and its only use both lived
        # in old_str and neither survives) escapes the gate; a bare deletion
        # with nothing replacing it does not. Gated by confirm_removals so
        # an intentional removal still goes through when the flag is set.
        dropped_defs = origin["_dropped_top_level_defs"](text, new_text)
        if path.suffix == ".py":
            dropped_vars = [
                name for name in origin["_dropped_top_level_vars"](text, new_text)
                if origin["_var_drop_is_confirmed_loss"](
                    name, args["old_str"], args["new_str"], orphaned)
            ]
        else:
            dropped_vars = []
        dropped = dropped_defs + dropped_vars
        if dropped and not args.get("confirm_removals"):
            return (
                f"ERROR: this edit to {args['path']} permanently removes these "
                f"top-level symbols in their entirety: {', '.join(dropped)}. "
                f"These may be public API consumed by other files. If this is "
                f"unintentional, keep the definition in new_str. If the removal "
                f"is intentional, repeat this exact call with "
                f"confirm_removals=true. The edit was NOT applied."
            )
        path.write_text(new_text)
        origin["_SYNTAX_REJECT_COUNTS"].pop(args["path"], None)
        return (f"edited {args['path']}" + (f" ({note})" if note else "")
                + origin["_lint_feedback_for"](args['path'], origin["CWD"]))
    if fn == "replace_lines":
        path = origin["CWD"] / args["path"]
        if not path.exists():
            return f"ERROR: {args['path']} does not exist (use create_file for new files)."
        start = args.get("start")
        end = args.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            return (f"ERROR: replace_lines requires integer start and end "
                    f"(got start={start!r}, end={end!r}).")
        if start < 1:
            return f"ERROR: line_start {start} must be >= 1 (1-indexed)."
        if end < start:
            return f"ERROR: line_end {end} is less than line_start {start}."
        old_text = path.read_text()
        lines = old_text.splitlines(keepends=True)
        if start > len(lines):
            return f"ERROR: line_start {start} is beyond {args['path']}'s {len(lines)} lines."
        # Optional stale-range anchors: verified when supplied, absent otherwise.
        # MUST stay optional - _str_replace_not_found_diag steers the model to
        # replace_lines when str_replace's old_str won't match; mandatory anchors
        # would close that escape hatch and strand a weak model with no edit path.
        anchor_err = origin["edit_guards"].verify_range_anchors(
            lines, start, end, args.get("expect_first"), args.get("expect_last"))
        if anchor_err:
            return f"ERROR: {anchor_err}\nThe edit was NOT applied."
        new_str = args.get("new_str", "")
        # Keep the block newline-terminated so we don't fuse the next line on.
        if new_str and not new_str.endswith("\n"):
            new_str = new_str + "\n"
        new_text = "".join(lines[:start - 1]) + new_str + "".join(lines[end:])
        err = origin["_python_syntax_error"](args["path"], new_text)
        note = None
        if err:
            repair = origin["_try_repair_indentation"](new_text)
            if repair is None:
                return origin["_record_syntax_rejection"](args["path"], err, len(old_text.splitlines()))
            new_text, note = repair
        orphaned = origin["_newly_undefined_names"](args["path"], old_text, new_text)
        if orphaned:
            return (
                f"ERROR: this edit to {args['path']} deletes the only assignment "
                f"to {', '.join(orphaned)} while a use of it survives elsewhere - "
                f"this will raise NameError/UnboundLocalError at runtime. Keep the "
                f"assignment, remove the surviving use too, or replace it with an "
                f"equivalent. The edit was NOT applied."
            )
        deletions, rewrites = origin["edit_guards"].classify_removed_lines(lines[start - 1:end], new_str)
        if deletions and not args.get("confirm_removals"):
            report = origin["edit_guards"].render_removal_report(deletions, rewrites)
            # Unconditional top-level-symbol-loss check: name any def/class/
            # constant that this range removes in its entirety, even when no
            # same-file reference survives (the symbol may be consumed by OTHER
            # files). This only ENRICHES the existing confirm_removals-gated
            # rejection message -- it is not a separate blocking gate, so
            # confirm_removals=true still short-circuits past it untouched.
            dropped = origin["_dropped_top_level_defs"](old_text, new_text)
            if path.suffix == ".py":
                dropped += origin["_dropped_top_level_vars"](old_text, new_text)
            symbol_note = ""
            if dropped:
                symbol_note = (
                    f"This edit also permanently removes these top-level symbols "
                    f"in their entirety: {', '.join(dropped)}\n"
                )
            return (
                f"ERROR: this edit to {args['path']} deletes {len(deletions)} line(s) "
                f"that don't appear to survive (as-is or rewritten) in your replacement:"
                f"{symbol_note}{report}\n\nRevise new_str to preserve these lines, or if the deletion "
                f"is intentional, repeat this exact call with confirm_removals=true. "
                f"The edit was NOT applied."
            )
        path.write_text(new_text)
        origin["_SYNTAX_REJECT_COUNTS"].pop(args["path"], None)
        removed_echo = origin["edit_guards"].render_removal_report([], rewrites)
        # Advisory only: warn if new_str duplicates a block that still lives
        # outside the replaced range. Computed from the ORIGINAL lines read
        # (prefix + suffix), so the range's own former content is not
        # counted as a duplicate. Does not block - the write above has already
        # landed.
        surrounding_text = "".join(lines[:start - 1]) + "".join(lines[end:])
        dup_warn = origin["edit_guards"].duplicated_block_warning(new_str, surrounding_text)
        return (f"edited {args['path']} (lines {start}-{end})" + (f" ({note})" if note else "")
                + removed_echo + dup_warn + origin["_lint_feedback_for"](args['path'], origin["CWD"]))
    if fn == "view_file":
        path = origin["CWD"] / args["path"]
        if not path.exists():
            return f"ERROR: {args['path']} does not exist."
        origin["_VIEWED_THIS_RUN"].add(args["path"])
        lines = path.read_text().splitlines(keepends=True)
        line_start, line_end = args.get("line_start"), args.get("line_end")
        if line_start is not None or line_end is not None:
            start = line_start if line_start is not None else 1
            end = line_end if line_end is not None else len(lines)
            if start < 1:
                return f"ERROR: line_start {start} must be >= 1 (1-indexed)."
            if start > len(lines):
                return f"ERROR: line_start {start} is beyond {args['path']}'s {len(lines)} lines."
            if end < start:
                return f"ERROR: line_end {end} is less than line_start {start}."
            selected = lines[start - 1:end]
            return "".join(f"{start + i:4d}| {ln}" for i, ln in enumerate(selected))
        formatted = "".join(f"{i + 1:4d}| {ln}" for i, ln in enumerate(lines))
        if len(formatted) <= 3000:
            return formatted
        return (
            formatted[:3000]
            + f"\n... [truncated; {args['path']} has {len(lines)} lines total — "
              f"call view_file again with line_start/line_end to see more]"
        )
    if fn == "restore_file":
        path_str = args.get("path", "")
        if not path_str:
            return "ERROR: restore_file requires a path."
        result = subprocess.run(
            ["git", "checkout", "HEAD", "--", path_str],
            check=False, cwd=origin["CWD"], capture_output=True, text=True,
        )
        if result.returncode != 0:
            return (f"ERROR: could not restore {path_str} to HEAD: "
                     f"{result.stderr.strip()[:300]}")
        return (f"restored {path_str} to its last commit (HEAD) — any "
                 f"uncommitted changes to this file are gone. Other files are untouched.")
    if fn == "bash":
        cmd = args.get("command", "")
        # Refuse destructive git ops before they reach the shell — they discard
        # the branch's WIP commits / working-tree changes (see
        # DESTRUCTIVE_GIT_PATTERNS). Ported from local_agent.py (Mode 3a).
        bad = origin["destructive_git_op"](cmd)
        if bad:
            return (
                f"ERROR: '{bad}' is blocked — it would discard your work (WIP "
                f"commits or uncommitted changes). To change a file, use "
                f"str_replace; to unstage, use `git reset HEAD <path>` (no "
                f"--hard). To undo a recent commit but keep the changes, use "
                f"`git reset HEAD~1` (default --mixed, keeps the working tree). "
                f"To throw away your OWN uncommitted edits to one specific file "
                f"and start it clean from the last commit, use the restore_file "
                f"tool on that path — it does exactly this, safely, without "
                f"touching any other file."
            )
        # Acquire the cross-dispatch heavy-build lock for any command whose
        # first token is a known build/test executable (cargo, npm, mvn,
        # etc.). See _heavy_lock docstring for the rationale.
        try:
            argv0 = shlex.split(cmd)[0] if cmd.strip() else ""
        except ValueError:
            argv0 = ""
        is_heavy = bool(argv0) and origin["p"]._is_heavy([argv0])
        run_kwargs = {"shell": True, "cwd": origin["CWD"], "capture_output": True, "text": True,
                          "timeout": origin["BASH_TIMEOUT"]}
        if is_heavy:
            with origin["p"]._heavy_lock():
                pr = subprocess.run(cmd, check=False, **run_kwargs)
        else:
            pr = subprocess.run(cmd, check=False, **run_kwargs)
        result = (pr.stdout + pr.stderr)[:3000] or "(no output)"
        if origin["ACCEPTANCE_PATHS"]:
            result += origin["_restore_tampered_oracle_files"]()
        return result
    if fn == "search":
        return (
            "unknown tool search — there is no search tool. Use bash with "
            "grep or rg to find code (e.g. `grep -n \"def foo\" -R .`), then "
            "view_file with line_start/line_end on the line number it reports."
        )
    return f"unknown tool {fn}"


def safe_run_tool_impl(origin, fn, args) -> str:
    """Run a tool, turning any exception into a recoverable error message.

    A model that omits a required argument (e.g. str_replace without old_str,
    observed with weaker local models) would otherwise raise an uncaught
    KeyError and crash the whole unattended agent. Feeding the error back as a
    tool result lets the model correct itself, bounded by the loop guard / step
    cap, instead of taking the run down.
    """
    try:
        # Routed via origin (NOT a direct run_tool_impl call) so a test that
        # monkeypatches scripts.local_agent_oracle.run_tool is honored here.
        return origin["run_tool"](fn, args)
    except Exception as e:  # noqa: BLE001 (a tool call's own failure is reported back to the model as tool output, not raised - the agent loop must never crash on an unpredictable tool error)
        return f"ERROR running {fn}: {type(e).__name__}: {e}"