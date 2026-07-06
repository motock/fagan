// Independent ground-truth grader for the interval-merge JavaScript
// task. Never written into the agent's worktree; only the harness's
// run_groundtruth copies this into a scratch dir and runs
// `node --test` against it. The agent's spec tells it to follow TDD,
// so it will also write its own test/merge.test.js against the same
// merge() export. This file adds cases the agent's tests typically
// miss:
//   - the +1 adjacency rule when the gap is > 1 (must NOT merge);
//   - the no-mutation guarantee on the inner arrays (acceptance
//     only checks the outer reference);
//   - a stress test that exercises a long chain of merges.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { merge } = require("../src/merge.js");

test("does NOT merge intervals with a gap of 2", () => {
  // [1,2] and [5,6] are 2 apart (gap at 3-4), must stay separate.
  assert.deepEqual(merge([[1, 2], [5, 6]]), [[1, 2], [5, 6]]);
});

test("does NOT mutate the inner interval arrays", () => {
  // Acceptance only checks the outer reference. A subtle bug is to
  // sort in place or to overwrite the inner arrays with the merged
  // result - the ground-truth catches that.
  const inner = [1, 2];
  const input = [[3, 4], inner];
  const result = merge(input);
  // inner must still be [1, 2]
  assert.deepEqual(inner, [1, 2]);
  // result must not BE the same reference as input
  assert.notEqual(result, input);
});

test("stress merges a long chain", () => {
  // Each interval is [i, i+1]; the next starts at i+1, which is
  // current.end + 1 = i+1, so by the +1 adjacency rule they all
  // merge into a single [0, 100] interval.
  const input = [];
  for (let i = 0; i < 100; i++) {
    input.push([i, i + 1]);
  }
  const result = merge(input);
  assert.equal(result.length, 1);
  assert.deepEqual(result[0], [0, 100]);
});

test("throws on start > end with a clear message", () => {
  // The acceptance allows any message matching /start.*end|invalid/i.
  // The groundtruth is stricter: the error must be thrown (not a
  // silent return) AND the message must include both 'start' and
  // 'end' so a human reading the failure can understand what went
  // wrong. An agent that returns a malformed result instead of
  // throwing will fail this test.
  assert.throws(() => merge([[5, 1]]), /start/i);
  assert.throws(() => merge([[5, 1]]), /end/i);
});

test("chain merge preserves order", () => {
  // [1,2] touches [3,5] and [6,7]; result is [1,7].
  assert.deepEqual(merge([[1, 2], [6, 7], [3, 5]]), [[1, 7]]);
});

test("fully overlapping intervals merge to outer bounds", () => {
  assert.deepEqual(merge([[1, 10], [3, 5], [2, 8]]), [[1, 10]]);
});
