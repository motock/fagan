#!/usr/bin/env python3
"""Throwaway carve script for PLD90-SS-2. Dry-run by default; --write applies."""
import builtins
import re
import subprocess
import symtable
import sys

SS = "pipeline/story_status.py"
DG = "pipeline/detached_grade.py"

START = '    grading_pid = story.get("grading_pid")\n'
END = '    if test_result is None:\n'
END_NEXT_PREFIX = "# Heavy build/test commands"

SIG = (
    "def _detached_grade_lifecycle(story, story_key, pid, worktree, manifest, "
    "manifest_path, test_cmd, test_dir, test_env):\n"
)
DOC = '''    """Run check_story_status's detached-grade lifecycle with pipeline.server's globals.

    Rebound onto pipeline.server.__dict__ in pipeline/story_status.py exactly like
    check_story_status, so globals().get("start_detached_grade") /
    globals().get("collect_detached_grade") / globals().get("_untrack_scratchpad")
    resolve to pipeline.server's (pytest-conditional) exports rather than this
    module's own.

    Returns (early_result, test_result): early_result is the dict
    check_story_status must return immediately, else None; test_result is None
    when the caller must grade synchronously.
    """
'''
TAIL = "    return None, test_result\n"

CALL = """    _early, test_result = _detached_grade_lifecycle(  # noqa: F821
        story, story_key, pid, worktree, manifest, manifest_path,
        test_cmd, test_dir, test_env,
    )
    if _early is not None:
        return _early
"""

IMPORT_LINE = (
    "    _detached_grade_lifecycle_unbound,\n"
)

REBIND = """# check_story_status's own logic (not a primitive): rebound onto pipeline.server's
# globals so its globals().get(...) probes see the same pytest-conditional exports
# check_story_status sees, and exported unconditionally.
_detached_grade_lifecycle = types.FunctionType(
    _detached_grade_lifecycle_unbound.__code__,
    _server.__dict__,
    _detached_grade_lifecycle_unbound.__name__,
    _detached_grade_lifecycle_unbound.__defaults__,
    _detached_grade_lifecycle_unbound.__closure__,
)
_server._detached_grade_lifecycle = _detached_grade_lifecycle
"""

PARAMS = [
    "story", "story_key", "pid", "worktree", "manifest", "manifest_path",
    "test_cmd", "test_dir", "test_env",
]
KNOWN_GLOBALS = {
    "os", "subprocess", "datetime", "timezone", "Path",
    "_atomic_write_json", "DETACHED_GRADE_WATCHDOG_SECONDS",
}


def find_block(lines):
    starts = [i for i, ln in enumerate(lines) if ln == START]
    assert len(starts) == 1, f"START anchor count={len(starts)}"
    s = starts[0]
    ends = [
        i for i, ln in enumerate(lines)
        if ln == END and i + 1 < len(lines) and lines[i + 1].lstrip().startswith(END_NEXT_PREFIX)
    ]
    assert len(ends) == 1, f"END anchor count={len(ends)}"
    e = ends[0]
    assert e > s, "END before START"
    return s, e


def free_names(block):
    src = "def _probe():\n" + block
    table = symtable.symtable(src, "<probe>", "exec")
    fn = next(c for c in table.get_children() if c.get_name() == "_probe")
    return {s.get_name() for s in fn.get_symbols() if s.is_global()}


def main():
    write = "--write" in sys.argv
    ss = open(SS).readlines()
    dg = open(DG).readlines()
    s, e = find_block(ss)
    block = ss[s:e]
    assert "global " not in "".join(block), "block contains a global statement"

    rets = [ln for ln in block if re.match(r"^\s*return \{.*\}$", ln)]
    print(f"block lines {s + 1}..{e} ({len(block)} lines)")
    print(f"return sites ({len(rets)}):")
    for ln in rets:
        print("   " + ln.rstrip("\n"))
    assert len(rets) == 4, f"expected 4 return sites, got {len(rets)}"

    names = free_names("".join(block))
    unknown = names - KNOWN_GLOBALS - set(dir(builtins))
    print("free names:", sorted(names))
    print("unknown (not builtins, not known globals):", sorted(unknown))
    assert unknown == set(PARAMS), f"unexpected free names: {sorted(unknown)}"
    print("params not read by block:", sorted(set(PARAMS) - names))

    print("wc -l before:", subprocess.run(
        ["wc", "-l", SS, DG], capture_output=True, text=True).stdout.strip())

    if not write:
        print("dry run only; pass --write to apply")
        return

    # (i) transform the 4 return lines
    new_block = []
    for ln in block:
        if re.match(r"^\s*return \{.*\}$", ln):
            new_block.append(ln.rstrip("\n") + ", None\n")
        else:
            new_block.append(ln)
    assert sum(1 for ln in new_block if ln.rstrip("\n").endswith("}, None")) == 4

    # (ii) append helper to detached_grade.py
    dg_out = list(dg)
    if dg_out and not dg_out[-1].endswith("\n"):
        dg_out[-1] += "\n"
    dg_out += ["\n", "\n", SIG, DOC] + new_block + [TAIL]
    open(DG, "w").writelines(dg_out)

    # (iii) replace block in story_status.py with the call
    ss_out = ss[:s] + [CALL] + ss[e:]

    # (iv) add the alias into the existing `from .detached_grade import (` block
    di = [i for i, ln in enumerate(ss_out) if ln.startswith("from .detached_grade import (")]
    assert len(di) == 1, f"detached_grade import block count={len(di)}"
    di = di[0]
    close = next(i for i in range(di, len(ss_out)) if ss_out[i] == ")\n")
    members = ss_out[di + 1:close]
    assert not any("_detached_grade_lifecycle_unbound" in m for m in members)
    ins = None
    for i, m in enumerate(members):
        if m.strip().split(",")[0] > "_detached_grade_lifecycle_unbound":
            ins = i
            break
    assert ins is not None, "no sorted insertion point found"
    ss_out = ss_out[:di + 1 + ins] + [IMPORT_LINE] + ss_out[di + 1 + ins:]

    # (v) rebind + export right after the existing check_story_status FunctionType stmt
    ci = [i for i, ln in enumerate(ss_out) if ln.startswith("check_story_status = types.FunctionType(")]
    assert len(ci) == 1, f"FunctionType rebind count={len(ci)}"
    ci = ci[0]
    close = next(i for i in range(ci, len(ss_out)) if ss_out[i] == ")\n")
    ss_out = ss_out[:close + 1] + [REBIND] + ss_out[close + 1:]

    open(SS, "w").writelines(ss_out)
    print("wc -l after:", subprocess.run(
        ["wc", "-l", SS, DG], capture_output=True, text=True).stdout.strip())


if __name__ == "__main__":
    main()
