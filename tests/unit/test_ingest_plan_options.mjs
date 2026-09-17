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
      if (!elements.has(id)) elements.set(id, makeElement());
      return elements.get(id);
    },
    createElement: makeElement,
    body: makeElement(),
  };
  return doc;
}

// Helper to flush microtasks
async function flush() {
  await new Promise(r => setTimeout(r, 0));
}

// Test harness setup function
import { runTest } from './runTest.js';

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
  assertEqual(html.includes('select a plan'), false);
  assertEqual(fetchCalls.length, 1);
});

// Test 3: rejected fetch
await test('populateIngestPlanOptions fetch error', async () => {
  let unhandled = false;
  const handler = () => { unhandled = true; };
  process.on('unhandledRejection', handler);
  const { mod, fetchCalls, doc } = await runTest({
    fetchResponse: new Error('boom'),
    expectFetchCount: 1,
  });
  await flush();
  const select = doc.getElementById('ingest-plan-name');
  const html = select.innerHTML;
  assertEqual(html, '<option value="" disabled selected>failed to load plans</option>');
  assertEqual(unhandled, false);
  assertEqual(fetchCalls.length, 1);
  process.removeListener('unhandledRejection', handler);
});

// Test 4: escaping
await test('populateIngestPlanOptions escaping', async () => {
  const { mod, fetchCalls, doc } = await runTest({
    fetchResponse: { plans: ['<img src=x onerror=alert(1)>'] },
    expectFetchCount: 1,
  });
  const select = doc.getElementById('ingest-plan-name');
  const html = select.innerHTML;
  assert(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert(!html.includes('<img'));
  assertEqual(fetchCalls.length, 1);
});

// Test 5: called once on load
await test('populateIngestPlanOptions called once on load', async () => {
  const { mod, fetchCalls, doc } = await runTest({
    fetchResponse: { plans: ['a'] },
    expectFetchCount: 1,
  });
  await flush();
  assertEqual(fetchCalls.length, 1);
  // call sendCommsMessage twice
  mod.sendCommsMessage('hi');
  mod.sendCommsMessage('hi');
  await flush();
  assertEqual(fetchCalls.length, 1);
});

// Test 6: element absent
await test('populateIngestPlanOptions element absent', async () => {
  const { mod, fetchCalls, doc } = await runTest({
    fetchResponse: { plans: ['a'] },
    elementAbsent: true,
    expectFetchCount: 0,
  });
  await flush();
  assertEqual(fetchCalls.length, 0);
});


// Helper to create a stub document with getElementById that can be overridden

// Helper to flush microtasks

// Test harness setup function
// duplicate import removed

// Helper to count occurrences of a substring
// duplicate count removed
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
  assertEqual(html.includes('select a plan'), false);
  assertEqual(fetchCalls.length, 1);
});

// Test 3: rejected fetch
await test('populateIngestPlanOptions fetch error', async () => {
  let unhandled = false;
  const handler = () => { unhandled = true; };
  process.on('unhandledRejection', handler);
  const { mod, fetchCalls, doc } = await runTest({
    fetchResponse: new Error('boom'),
    expectFetchCount: 1,
  });
  await flush();
  const select = doc.getElementById('ingest-plan-name');
  const html = select.innerHTML;
  assertEqual(html, '<option value="" disabled selected>failed to load plans</option>');
  assertEqual(unhandled, false);
  assertEqual(fetchCalls.length, 1);
  process.removeListener('unhandledRejection', handler);
});

// Test 4: escaping
await test('populateIngestPlanOptions escaping', async () => {
  const { mod, fetchCalls, doc } = await runTest({
    fetchResponse: { plans: ['<img src=x onerror=alert(1)>'] },
    expectFetchCount: 1,
  });
  const select = doc.getElementById('ingest-plan-name');
  const html = select.innerHTML;
  assert(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert(!html.includes('<img'));
  assertEqual(fetchCalls.length, 1);
});

// Test 5: called once on load
await test('populateIngestPlanOptions called once on load', async () => {
  const { mod, fetchCalls, doc } = await runTest({
    fetchResponse: { plans: ['a'] },
    expectFetchCount: 1,
  });
  await flush();
  assertEqual(fetchCalls.length, 1);
  // call sendCommsMessage twice
  mod.sendCommsMessage('hi');
  mod.sendCommsMessage('hi');
  await flush();
  assertEqual(fetchCalls.length, 1);
});

// Test 6: element absent
await test('populateIngestPlanOptions element absent', async () => {
  const { mod, fetchCalls, doc } = await runTest({
    fetchResponse: { plans: ['a'] },
    elementAbsent: true,
    expectFetchCount: 0,
  });
  await flush();
  assertEqual(fetchCalls.length, 0);
});
