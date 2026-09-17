import { test } from 'node:test';
import assert from 'node:assert/strict';

// ---------------------------------------------------------------------------
// Harness: stub the browser globals static/app/comms.js touches at import time
// (document, window, localStorage, fetch), then import the real module with a
// cache-busting query so each test gets a fresh module evaluation (and thus a
// fresh populateIngestPlanOptions() call).
// ---------------------------------------------------------------------------

function makeElement() {
  return {
    _innerHTML: '',
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) { this._innerHTML = v; },
    textContent: '',
    value: '',
    disabled: false,
    addEventListener() {},
    setAttribute() {},
    getAttribute() { return null; },
    removeAttribute() {},
    appendChild() {},
    classList: { add() {}, remove() {}, toggle() {} },
    style: {},
    children: [],
    dataset: {},
  };
}

function makeDoc({ withPlanSelect = true } = {}) {
  const elements = new Map();
  if (withPlanSelect) {
    elements.set('ingest-plan-name', makeElement());
  } else {
    // Explicit null: getElementById must return null/undefined for the plan
    // select so the element-absent negative test is meaningful, while other
    // ids still get generic stubs.
    elements.set('ingest-plan-name', null);
  }
  const doc = {
    getElementById(id) {
      if (!elements.has(id)) {
        // Generic stub so unrelated module-load wiring (comms thread, send
        // button, etc.) never throws; only 'ingest-plan-name' is asserted on.
        elements.set(id, makeElement());
      }
      return elements.get(id);
    },
    createElement: () => makeElement(),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    body: makeElement(),
  };
  return { doc, elements };
}

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

async function loadComms({ fetchResponse, withPlanSelect = true } = {}) {
  const { doc, elements } = makeDoc({ withPlanSelect });
  globalThis.document = doc;
  globalThis.window = { __PIPELINE_API_KEY__: 'test-key' };
  globalThis.localStorage = {
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {},
  };
  const fetchCalls = [];
  globalThis.fetch = async (url, opts) => {
    fetchCalls.push({ url, opts });
    if (fetchResponse instanceof Error) throw fetchResponse;
    return { ok: true, json: async () => fetchResponse };
  };
  const mod = await import('../../static/app/comms.js?case=' + Math.random());
  await flush();
  return { mod, fetchCalls, doc, select: elements.get('ingest-plan-name') };
}

// Count only calls to the available-plans endpoint; other endpoints fetched by
// unrelated module code (e.g. sendCommsMessage's POST) must not skew counts.
function planFetchCount(fetchCalls) {
  return fetchCalls.filter((c) => String(c.url).includes('/api/ingestable-plans')).length;
}

function count(haystack, needle) {
  return haystack.split(needle).length - 1;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test('populateIngestPlanOptions: success renders one option per plan plus placeholder', async () => {
  const { fetchCalls, select } = await loadComms({
    fetchResponse: { plans: ['anagram', 'foo'] },
  });
  assert.equal(fetchCalls.length, 1, 'fetchIngestablePlans called exactly once');
  assert.equal(fetchCalls[0].url, '/api/ingestable-plans');
  const html = select.innerHTML;
  assert.equal(count(html, '<option'), 3, 'placeholder + 2 plans');
  assert.equal(
    html,
    '<option value="" disabled selected>select a plan\u2026</option>'
      + '<option value="anagram">anagram</option>'
      + '<option value="foo">foo</option>'
  );
  assert.equal(count(html, 'disabled'), 1, 'only the placeholder is disabled');
});

test('populateIngestPlanOptions: empty plans renders single disabled placeholder', async () => {
  const { fetchCalls, select } = await loadComms({ fetchResponse: { plans: [] } });
  assert.equal(fetchCalls.length, 1);
  const html = select.innerHTML;
  assert.equal(html, '<option value="" disabled selected>no plans found</option>');
  assert.equal(count(html, '<option'), 1);
});

test('populateIngestPlanOptions: rejected fetch is caught and renders failure option', async () => {
  const { fetchCalls, select } = await loadComms({
    fetchResponse: new Error('boom'),
  });
  assert.equal(fetchCalls.length, 1);
  const html = select.innerHTML;
  assert.equal(html, '<option value="" disabled selected>failed to load plans</option>');
  assert.equal(count(html, '<option'), 1);
});

test('populateIngestPlanOptions: plan names are HTML-escaped', async () => {
  const { select } = await loadComms({
    fetchResponse: { plans: ['<img src=x onerror=alert(1)>'] },
  });
  const html = select.innerHTML;
  assert.equal(html.includes('<img src=x onerror=alert(1)>'), false, 'raw tag must not appear');
  assert.ok(html.includes('&lt;img'), 'escaped markup present');
  assert.equal(count(html, '<option'), 2, 'placeholder + 1 escaped plan');
});

test('populateIngestPlanOptions: called exactly once at module load, not per sendCommsMessage', async () => {
  const { mod, fetchCalls } = await loadComms({
    fetchResponse: { plans: ['anagram'] },
  });
  assert.equal(fetchCalls.length, 1, 'exactly one call at module load');
  if (typeof mod.sendCommsMessage === 'function') {
    await mod.sendCommsMessage('hello');
    await flush();
    assert.equal(fetchCalls.length, 1, 'no additional call from sendCommsMessage');
  }
});

test('populateIngestPlanOptions: missing select element is a no-op (no fetch, no throw)', async () => {
  const { fetchCalls } = await loadComms({
    fetchResponse: { plans: ['anagram'] },
    withPlanSelect: false,
  });
  await flush();
  assert.equal(fetchCalls.length, 0, 'fetchIngestablePlans must not be called');
});
