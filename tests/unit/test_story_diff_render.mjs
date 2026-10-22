// Tests for static/app/render/story-diff.js — the story worktree-diff renderer.
//
// Run with:  node tests/unit/test_story_diff_render.mjs

import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const MODULE_PATH = path.join(HERE, "..", "..", "static", "app", "render", "story-diff.js");
const { renderStoryDiffSummary, renderStoryDiffFile } = await import(
  `${pathToFileURL(MODULE_PATH).href}?t=${Date.now()}`
);

const EVIL = "<img src=x onerror=alert(1)>";

function summaryFixture(overrides = {}) {
  return {
    available: true,
    reason: null,
    totals: { files: 3, added: 12, removed: 4, untracked: 1, committed: 2, working: 1 },
    truncated: false,
    files: [
      { path: "a.py", status: "M", added: 5, removed: 4, binary: false, untracked: false },
      { path: "b.py", status: "A", added: 7, removed: 0, binary: false, untracked: false },
      { path: "c.txt", status: "?", added: 0, removed: 0, binary: false, untracked: true },
    ],
    ...overrides,
  };
}

let passed = 0;
const failures = [];
function test(name, fn) {
  try {
    fn();
    passed += 1;
  } catch (err) {
    failures.push(`${name}: ${err.message}`);
  }
}

test("should_escape_malicious_path_in_summary", () => {
  const html = renderStoryDiffSummary(
    summaryFixture({ files: [{ path: EVIL, status: "M", added: 1, removed: 0, untracked: false }] }),
  );
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes("&lt;img src=x onerror=alert(1)&gt;"));
});

test("should_escape_malicious_old_path_in_summary", () => {
  const html = renderStoryDiffSummary(
    summaryFixture({
      files: [{ path: "n.py", old_path: EVIL, status: "R", added: 0, removed: 0, untracked: false }],
    }),
  );
  assert.ok(!html.includes("<img"));
});

test("should_escape_malicious_diff_line_in_file", () => {
  const html = renderStoryDiffFile({ available: true, path: "a.py", diff: `+${EVIL}`, truncated: false });
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes("&lt;img"));
});

test("should_escape_malicious_path_in_file_header", () => {
  const html = renderStoryDiffFile({ available: true, path: EVIL, diff: "+x", truncated: false });
  assert.ok(!html.includes("<img"));
});

test("should_render_totals", () => {
  const html = renderStoryDiffSummary(summaryFixture());
  assert.ok(html.includes("3 files"));
  assert.ok(html.includes("+12"));
  assert.ok(html.includes("-4"));
});

test("should_render_per_file_counts_and_status_badges", () => {
  const html = renderStoryDiffSummary(summaryFixture());
  assert.ok(html.includes("story-diff-status-M"));
  assert.ok(html.includes("story-diff-status-A"));
  assert.ok(html.includes("+5"));
  assert.ok(html.includes("-4"));
  assert.ok(html.includes("+7"));
});

test("should_mark_untracked_files", () => {
  const html = renderStoryDiffSummary(summaryFixture());
  assert.ok(html.includes("untracked"));
});

test("should_not_mark_untracked_when_none", () => {
  const html = renderStoryDiffSummary(
    summaryFixture({ files: [{ path: "a.py", status: "M", added: 1, removed: 0, untracked: false }] }),
  );
  assert.ok(!html.includes("untracked"));
});

test("should_show_truncation_notice_on_truncated_summary", () => {
  assert.ok(renderStoryDiffSummary(summaryFixture({ truncated: true })).includes("truncated"));
});

test("should_not_show_truncation_notice_when_complete", () => {
  assert.ok(!renderStoryDiffSummary(summaryFixture()).includes("truncated"));
});

test("should_show_truncation_notice_on_truncated_file_diff", () => {
  const html = renderStoryDiffFile({
    available: true, path: "a.py", diff: "+x", truncated: true, note: "diff truncated to the first 5 of 9 characters",
  });
  assert.ok(html.includes("diff truncated to the first 5 of 9 characters"));
});

test("should_render_diff_lines_with_classes", () => {
  const html = renderStoryDiffFile({ available: true, path: "a.py", diff: "+a\n-b", truncated: false });
  assert.ok(html.includes("diff-add"));
  assert.ok(html.includes("diff-del"));
});

test("should_render_binary_file_notice", () => {
  const html = renderStoryDiffFile({ available: true, path: "a.bin", diff: "", binary: true, truncated: false });
  assert.ok(html.includes("binary"));
});

for (const reason of ["no_worktree", "worktree_gone", "not_a_worktree", "no_base_ref", "git_failed"]) {
  test(`should_render_empty_state_for_${reason}_in_summary`, () => {
    const html = renderStoryDiffSummary({ available: false, reason, files: [] });
    assert.ok(html.includes(`story-diff-empty-${reason}`));
    assert.ok(!html.includes("story-diff-files"));
  });
  test(`should_render_empty_state_for_${reason}_in_file`, () => {
    const html = renderStoryDiffFile({ available: false, reason });
    assert.ok(html.includes(`story-diff-empty-${reason}`));
  });
}

test("should_render_generic_empty_state_for_unknown_reason", () => {
  const html = renderStoryDiffSummary({ available: false, reason: EVIL, files: [] });
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes("story-diff-empty"));
});

test("should_not_throw_on_missing_input", () => {
  assert.equal(typeof renderStoryDiffSummary(undefined), "string");
  assert.equal(typeof renderStoryDiffFile(null), "string");
});

if (failures.length) {
  console.error(failures.join("\n"));
  console.error(`${passed} passed, ${failures.length} failed`);
  process.exit(1);
}
console.log(`${passed} passed, 0 failed`);
