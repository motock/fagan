import { test, assert } from 'node:test';
import assertEqual from 'node:assert/strict';

// Helper to create a stub element with innerHTML and optional textContent
function makeElement() {
  const el = {
    _innerHTML: '',
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) { this._innerHTML = v; },
    get textContent() { return this._innerHTML; },
    set textContent(v) { this._innerHTML = v; },
  };
  return el;
}

// Helper to create a stub document with getElementById that can be overridden
function makeDoc() {
  const elements = new Map();
  const doc = {
    getElementById(id) {
      return elements.get(id) || null;
    },
    querySelector: () => makeElement(),
    querySelectorAll: () => [],
  };
  return { doc, elements };
}

// Helper to flush microtasks
function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

// Test harness setup function
async function runTest({ fetchResponse, elementAbsent, expectFetchCount }) {
  const { doc, elements } = makeDoc();
  if (elementAbsent) {
    // Remove the element from the document
    elements.delete('ingest-plan-name');
  } else {
    elements.set('ingest-plan-name', makeElement());
  }
  const fetchCalls = [];
  const recorderFetch = async (url, opts) => {
    fetchCalls.push({ url, opts });
    return fetchResponse;
  };
  globalThis.fetch = recorderFetch;
  const mod = await import('./comms.js');
  return { mod, fetchCalls, doc };
}

// Helper to count occurrences of a substring
function count(str, sub) {
  return (str.match(new RegExp(sub, 'g')) || []).length;
}

// Test 1: success fixture
await test('populateIngestPlanOptions success', async () => {
  const { mod, fetchCalls, doc } = await runTest({
    fetchResponse: { plans: ['anagram', 'foo'] },
    expectFetchCount: 1,
  });
  const select = doc.getElementById('ingest-plan-name');
  const html = select.innerHTML;
  assertEqual(html, '<option value="" disabled selected>select a plan…</option><option value="anagram">anagram</option><option value="foo">foo</option>');
  assertEqual(count(html, '<option'), 3);
  assertEqual(count(html, 'disabled'), 1);
  assertEqual(fetchCalls.length, 1);
});

// Test 2: empty fixture
await test('populateIngestPlanOptions empty', async () => {
  const { mod, fetchCalls, doc } = await runTest({
    fetchResponse: { plans: [] },
    expectFetchCount: 1,
  });
  const select = doc.getElementById('ingest-plan-name');
  const html = select.innerHTML;
  assertEqual(html, '<option value="" disabled selected>no plans found</option>');
  assertEqual(count(html, '<option'), 1);
  assertEqual(count(html, 'disabled'), 1);
  assertEqual(fetchCalls.length, 1);
});

// Test 3: error fixture
await test('populateIngestPlanOptions error', async () => {
  const { mod, fetchCalls, doc } = await runTest({
    fetchResponse: Promise.reject(new Error('boom')),
    expectFetchCount: 1,
  });
  const select = doc.getElementById('ingest-plan-name');
  const html = select.innerHTML;
  assertEqual(html, '<option value="" disabled selected>failed to load plans</option>');
  assertEqual(count(html, '<option'), 1);
  assertEqual(count(html, 'disabled'), 1);
  assertEqual(fetchCalls.length, 1);
});

// Test 4: XSS test
await test('populateIngestPlanOptions XSS', async () => {
  const { mod, fetchCalls, doc } = await runTest({
    fetchResponse: { plans: ['<img src=x onerror=alert(1)>'] },
    expectFetchCount: 1,
  });
  const select = doc.getElementById('ingest-plan-name');
  const html = select.innerHTML;
  assertEqual(html.includes('<img src=x onerror=alert(1)>'), false);
  assertEqual(count(html, '<option'), 2);
  assertEqual(count(html, 'disabled'), 1);
  assertEqual(fetchCalls.length, 1);
});

// Test 5: element absent
await test('populateIngestPlanOptions element absent', async () => {
  const { mod, fetchCalls, doc } = await runTest({
    fetchResponse: { plans: ['a'] },
    elementAbsent: true,
    expectFetchCount: 0,
  });
  await flush();
  assertEqual(fetchCalls.length, 0);
});
