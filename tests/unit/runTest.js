export function makeElement() {
  const el = {
    _innerHTML: '',
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) { this._innerHTML = v; },
    get textContent() { return this._innerHTML; },
    set textContent(v) { this._innerHTML = v; },
  };
  return el;
}

export function makeDoc() {
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

export async function flush() {
  await new Promise(r => setTimeout(r, 0));
}

export async function runTest({ fetchResponse, elementAbsent = false, expectFetchCount, description }) {
  const doc = makeDoc();
  if (elementAbsent) {
    doc.getElementById = () => undefined;
  }
  globalThis.document = doc;
  const fetchCalls = [];
  globalThis.fetch = async (url) => {
    fetchCalls.push(url);
    if (fetchResponse instanceof Error) {
      return Promise.reject(fetchResponse);
    }
    return {
      ok: true,
      json: async () => fetchResponse,
    };
  };
  globalThis.window = { __PIPELINE_API_KEY__: 'test-key' };
  const mod = await import('../static/app/comms.js?case=' + Math.random());
  await flush();
  return { mod, fetchCalls, doc };
}
