// Visible acceptance oracle for the interval-merge JavaScript task.
// Node's built-in `node --test` runner auto-discovers *.test.js files
// under test/, so this file is picked up by `node --test test/` without
// any configuration. The agent is told in spec.json's agent_instructions
// to write test/merge.test.js FIRST (TDD); this file is "another set of
// acceptance cases" the agent's tests must also pass against. The
// ground-truth (test/groundtruth.test.js) is a separate independent
// grader that the agent never sees; that's what the benchmark actually
// grades on.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { merge } = require("../src/merge.js");

test("merges overlapping intervals", () => {
  assert.deepEqual(merge([[1, 3], [2, 6]]), [[1, 6]]);
});

test("merges adjacent (touching) intervals", () => {
  // Endpoints are inclusive, so [1,2] and [3,4] touch at 2-3.
  assert.deepEqual(merge([[1, 2], [3, 4]]), [[1, 4]]);
});

test("handles unsorted input", () => {
  // [8,9] is well past the +1 adjacency boundary for [1,4] (4+1=5, 8 > 5),
  // so the two result groups must stay disjoint.
  assert.deepEqual(merge([[8, 9], [1, 3], [2, 4]]), [[1, 4], [8, 9]]);
});

test("handles duplicates", () => {
  assert.deepEqual(merge([[1, 3], [1, 3], [2, 4]]), [[1, 4]]);
});

test("empty input returns empty array", () => {
  assert.deepEqual(merge([]), []);
});

test("single interval returns one-element array", () => {
  assert.deepEqual(merge([[1, 5]]), [[1, 5]]);
});

test("does not mutate the input", () => {
  const input = [[3, 4], [1, 2]];
  const snapshot = JSON.parse(JSON.stringify(input));
  merge(input);
  assert.deepEqual(input, snapshot);
});

test("throws on start > end", () => {
  assert.throws(() => merge([[3, 1]]), /start.*end|invalid/i);
});

test("disjoint intervals stay separate", () => {
  assert.deepEqual(merge([[1, 2], [10, 12], [4, 5]]), [[1, 2], [4, 5], [10, 12]]);
});
