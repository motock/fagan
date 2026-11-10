#!/usr/bin/env python3
"""Lint a plan's story `files` lists against the briefs, before ingest.

Usage: check_plan_files.py PLAN.json|PLAN.manifest.json [--repo DIR]

Catches the three authoring misses that park stories on the scope gate
(see pipeline-story-schema.md, "Declare `files` from where the code lives"):

  DOC      a doc a brief edits ("Docs: ...", "gains", "append") that is not in
           the story's `files`
  NAMED    any other non-test production path the brief names but `files`
           omits (a reference or an edit; judge by the printed context)
  OVERLAP  two stories with no dependency path between them that share a
           declared file (they conflict at the merge gate)
  HEADROOM (with --repo) a declared .py within 50 lines of the 1000-line cap

A story with no `files` is ungated and skipped. Exit 1 on DOC or OVERLAP.
"""
import itertools
import json
import re
import sys
from pathlib import Path

PATH = re.compile(
    r"\b(?:pipeline|app|docs|tools|deploy|static)/[\w./-]+\.(?:py|md|json|ya?ml|sh|js|html)"
    r"|\b[A-Z][A-Z_]+\.md"
)
DOCS_SEG = re.compile(r"Docs?[^:\n]{0,40}:(.{0,800})")


def _is_test(path: str) -> bool:
    return path.startswith(("tests/", "test_")) or "/test_" in path


def _stories(plan: dict) -> list[dict]:
    raw = plan.get("stories")
    if isinstance(raw, dict):
        return list(raw.values())
    return [s for e in plan.get("epics", []) for s in e.get("stories", [])]


def _ancestors(story, by_summary, seen):
    for dep in story.get("dependencies", []):
        if dep in by_summary and dep not in seen:
            seen.add(dep)
            _ancestors(by_summary[dep], by_summary, seen)
    return seen


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    plan = json.loads(Path(argv[0]).read_text())
    repo = Path(argv[argv.index("--repo") + 1]) if "--repo" in argv else None
    stories = _stories(plan)
    by_summary = {s["summary"]: s for s in stories}
    failed = False
    for s in stories:
        files = set(s.get("files") or [])
        if not files:
            continue
        brief = (s.get("agent_instructions") or "") + " " + (s.get("description") or "")
        docs = {p for seg in DOCS_SEG.findall(brief) for p in PATH.findall(seg)}
        for p in sorted(docs - files):
            print(f"DOC      {s['summary'][:60]} :: {p}")
            failed = True
        for p in sorted(set(PATH.findall(brief)) - files - docs):
            if _is_test(p):
                continue
            i = brief.find(p)
            ctx = brief[max(0, i - 70) : i + 60].replace("\n", " ")
            print(f"NAMED    {s['summary'][:60]} :: {p} :: ...{ctx}...")
        if repo:
            for f in sorted(files):
                path = repo / f
                if f.endswith(".py") and path.is_file():
                    n = len(path.read_text().splitlines())
                    if n >= 950:
                        print(f"HEADROOM {s['summary'][:60]} :: {f} is {n} lines")
    anc = {s["summary"]: _ancestors(s, by_summary, set()) for s in stories}
    for a, b in itertools.combinations(stories, 2):
        if b["summary"] in anc[a["summary"]] or a["summary"] in anc[b["summary"]]:
            continue
        common = set(a.get("files") or []) & set(b.get("files") or [])
        if common:
            print(f"OVERLAP  {a['summary'][:40]} <-> {b['summary'][:40]} :: {sorted(common)}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
