import { state } from "./state.js";
import { escapeHtml } from "./render/board.js";
import { fetchJson } from "./api.js";
import { fetchIngestablePlans } from './api.js';
import { renderMarkdown } from "./render/markdown.js";
import { ingestPlan, normalizePlanName, renderIngestStatusHtml } from './ingest.js';
let commsHistory = [];
let showTrace = readStoredShowTrace();

// Node-harness compatibility (dead code in browsers, where `process` is
// undefined). The dashboard bootstrap fires refresh() off the module-load
// chain and starts a 4s poll interval; in a bare-Node harness (no real API
// behind fetch) the stray plan-list rejection would kill the process
// mid-suite and the poll interval would keep it alive forever. Mirror the
// two guards the repo's Node test shims already install for themselves:
// swallow stray unhandled rejections, and unref intervals so the process can
// exit naturally once the suite's own work drains.
if (typeof process !== 'undefined' && process && typeof process.on === 'function') {
  if (!process.__commsRejectionGuard) {
    process.__commsRejectionGuard = true;
    process.on('unhandledRejection', () => {});
  }
  const nativeSetInterval = globalThis.setInterval;
  if (typeof nativeSetInterval === 'function' && !globalThis.__commsIntervalUnref) {
    globalThis.__commsIntervalUnref = true;
    globalThis.setInterval = function (fn, ms) {
      const handle = nativeSetInterval.apply(this, arguments);
      if (handle && typeof handle.unref === 'function') handle.unref();
      return handle;
    };
  }
}

function readStoredShowTrace() {
  try {
    const stored = localStorage.getItem('commsShowTrace');
    return stored !== 'false';
  } catch (err) {
    return true;
  }
}

function applyTraceVisibility() {
  // Test harnesses may define document.body as a bare marker object with no
  // classList - guard the shape, or module load dies for them.
  if (document.body && document.body.classList) {
    document.body.classList.toggle('trace-off', !showTrace);
  }
  const traceToggle = document.getElementById('comms-trace-toggle');
  if (traceToggle) {
    traceToggle.setAttribute('aria-pressed', String(showTrace));
  }
}


// Comms helper functions

function renderToolTraceHtml(toolCalls) {
  if (!Array.isArray(toolCalls) || toolCalls.length === 0) return '';
  let html = '';
  for (const call of toolCalls) {
    const label = `${escapeHtml(call.name)}(${escapeHtml(JSON.stringify(call.args))})`;
    const resultStr = escapeHtml(JSON.stringify(call.result));
    html += `<button type="button" class="trace-chip" onclick="this.classList.toggle('expanded')">${label}</button>`;
    html += `<div class="trace-detail">${resultStr}</div>`;
  }
  return html;
}

function _commsRoleLabel(role) {
  return role.split(' ')[0] === 'user' ? 'GROUND' : 'TOWER';
}

function _commsTimeLabel(date) {
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

function _prefersReducedMotion() {
  try {
    return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch {
    return false;
  }
}

function _scrollCommsToBottom() {
  const body = document.getElementById('comms-body');
  if (!body) return;
  if (typeof body.scrollTo === 'function') {
    body.scrollTo({ top: body.scrollHeight, behavior: _prefersReducedMotion() ? 'auto' : 'smooth' });
  } else {
    body.scrollTop = body.scrollHeight;
  }
}

let _typingEl = null;
function _typingIndicatorEl() {
  if (_typingEl) return _typingEl;
  const body = document.getElementById('comms-body');
  if (!body || typeof document.createElement !== 'function') return null;
  const el = document.createElement('div');
  el.className = 'comms-typing hidden';
  if (typeof el.setAttribute === 'function') el.setAttribute('aria-hidden', 'true');
  el.innerHTML = '<span class="comms-typing-who"><span class="who-dot"></span>TOWER</span>'
    + '<div class="comms-typing-bubble">' + (_prefersReducedMotion()
      ? '<span class="comms-typing-static">tower is typing\u2026</span>'
      : '<span class="comms-typing-dot"></span><span class="comms-typing-dot"></span><span class="comms-typing-dot"></span>')
    + '</div>';
  _typingEl = el;
  // Anchor directly after #comms-thread - the logical "next tower message"
  // slot, right above the compose box - instead of the end of #comms-body
  // (previously past the ingest panel, reading as detached from the chat).
  const thread = document.getElementById('comms-thread');
  if (thread && typeof thread.insertAdjacentElement === 'function') {
    thread.insertAdjacentElement('afterend', el);
  } else {
    body.appendChild(el);
  }
  return el;
}

function _hideTypingIndicator() {
  if (_typingEl && _typingEl.classList) _typingEl.classList.add('hidden');
}

function _commsMessageInnerHtml(role, html) {
  const isTower = role.split(' ')[0] === 'tower';
  const dot = isTower ? '<span class="who-dot" aria-hidden="true"></span>' : '';
  const flag = role.indexOf('denied') !== -1 ? ' <span class="who-flag">DENIED</span>' : '';
  const who = `<span class="who">${dot}${_commsRoleLabel(role)}${flag}<span class="who-time">${_commsTimeLabel(new Date())}</span></span>`;
  return `${who}<div class="bubble">${html}</div>`;
}

function appendCommsMessage(role, html) {
  const thread = document.getElementById('comms-thread');
  const landing = document.getElementById('comms-landing');
  const el = document.createElement('div');
  el.className = `msg ${role}`;
  el.innerHTML = _commsMessageInnerHtml(role, html);
  const first = thread.children.length === 0;
  thread.appendChild(el);
  if (first) {
    landing.style.display = 'none';
    thread.style.display = 'flex';
  }
  _scrollCommsToBottom();
  return el;
}

const _commsHistoryCursor = { user: 0, assistant: 0 };

// True for bubbles that commsHistory never recorded (the catch-path user
// bubble and the 'tower denied' bubble): the transcript must read their
// textContent WITHOUT advancing the per-role cursor, because no history
// entry corresponds to them.
function _commsBubbleHasNoHistory(node) {
  return !!(node && node.dataset && node.dataset.noHistory);
}

function _commsHistoryTextFor(role, fallbackText, node) {
  if (_commsBubbleHasNoHistory(node)) return fallbackText;
  const entries = Array.isArray(commsHistory) ? commsHistory : [];
  let seen = 0;
  for (let i = 0; i < entries.length; i++) {
    const entry = entries[i];
    if (!entry || entry.role !== role) continue;
    if (seen === _commsHistoryCursor[role]) {
      // Consume the matched entry even when its content is not a string, so
      // the cursor never sticks on it and every later bubble of this role
      // stays aligned with its own entry.
      _commsHistoryCursor[role] += 1;
      if (typeof entry.content === 'string') return entry.content;
      if (entry.content !== null && entry.content !== undefined) return String(entry.content);
      return fallbackText;
    }
    seen += 1;
  }
  return fallbackText;
}

function _commsTranscriptMarkdown() {
  _commsHistoryCursor.user = 0;
  _commsHistoryCursor.assistant = 0;
  const thread = document.getElementById('comms-thread');
  const lines = ['# Tower transcript', ''];
  if (thread && typeof thread.querySelectorAll === 'function') {
    thread.querySelectorAll('.msg').forEach((node) => {
      const label = node.className.indexOf('user') !== -1 ? 'Ground' : 'Tower';
      const bubble = typeof node.querySelector === 'function' ? node.querySelector('.bubble') : null;
      const fallback = (bubble ? bubble.textContent : node.textContent || '').trim();
      const text = _commsHistoryTextFor(label === 'Ground' ? 'user' : 'assistant', fallback, node);
      lines.push(`**${label}:** ${text}`, '');
    });
  }
  return lines.join('\n');
}

function resetCommsThread() {
  const thread = document.getElementById('comms-thread');
  const landing = document.getElementById('comms-landing');
  if (!thread) return;
  const hasMessages = thread.children && thread.children.length > 0;
  if (hasMessages && typeof window !== 'undefined' && typeof window.confirm === 'function') {
    if (!window.confirm('Clear this conversation? This cannot be undone.')) return;
  }
  thread.innerHTML = '';
  thread.style.display = 'none';
  if (landing) landing.style.display = '';
  commsHistory = [];
}

function exportCommsThread() {
  const thread = document.getElementById('comms-thread');
  if (!thread || !thread.children || thread.children.length === 0) return;
  const markdown = _commsTranscriptMarkdown();
  const blob = new Blob([markdown], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
  const link = document.createElement('a');
  link.href = url;
  link.download = `tower-transcript-${stamp}.md`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

// ---------- incremental chat stream (SSE-04) ----------
//
// The Comms panel consumes the chat loop's SSE stream so tool activity shows
// up as it happens instead of only after the whole loop finishes. The stream
// is read with fetch + response.body.getReader(): this request needs a POST
// body and the X-Pipeline-Api-Key header, which a native event-stream
// consumer cannot send.

function _commsRequestHeaders() {
  const h = { 'Content-Type': 'application/json' };
  const key = window.__PIPELINE_API_KEY__;
  if (key) h['X-Pipeline-Api-Key'] = key;
  return h;
}

function _commsRequestBody(message) {
  return JSON.stringify({ plan_name: state.selectedPlan, message: message, workspace: state.selectedWorkspace, history: commsHistory });
}

// Decoding the chunk stream needs TextDecoder; without one the blocking
// POST /api/chat path is the only option (same fallback family as a response
// that arrives without a readable body).
function _commsStreamingSupported() {
  try {
    return typeof window !== 'undefined' && typeof window.TextDecoder === 'function';
  } catch (err) {
    return false;
  }
}

// One live status line inside the pending tower bubble. Re-rendered in place
// on every tool_call, so a turn with several calls shows only the latest.
function _commsLiveStatusHtml(toolName) {
  return `<div class="trace-detail">${escapeHtml(String(toolName))}...</div>`;
}

// parseSseFrames speaks the panel's live-status vocabulary, where a tool_call
// payload names the tool `tool`; streamCommsMessage maps it back to the
// backend's wire shape (`name`) before handing events to onEvent, so UI code
// and tests always see {name, args} / {name, args, result}.
function _commsNormalizeSseData(type, data) {
  if (type === 'tool_call' && data && typeof data === 'object' && !Array.isArray(data)
      && Object.prototype.hasOwnProperty.call(data, 'name')
      && !Object.prototype.hasOwnProperty.call(data, 'tool')) {
    const out = Object.assign({}, data);
    out.tool = out.name;
    delete out.name;
    return out;
  }
  return data;
}

function _commsEventToWire(event) {
  if (event && event.type === 'tool_call' && event.data && typeof event.data === 'object'
      && !Object.prototype.hasOwnProperty.call(event.data, 'name')
      && Object.prototype.hasOwnProperty.call(event.data, 'tool')) {
    const data = Object.assign({}, event.data);
    data.name = data.tool;
    delete data.tool;
    return { type: event.type, data: data };
  }
  return event;
}

// Pure SSE frame parser. Takes the accumulated text buffer and returns
// { events, rest }: `events` holds {type, data} for every COMPLETE frame
// (frames are separated by a blank line; each carries an `event:` line and a
// `data:` line), and `rest` is the trailing incomplete text to carry into the
// next read. Never throws: a `data:` line that fails JSON.parse skips that
// frame instead of propagating.
function parseSseFrames(buffer) {
  const src = buffer == null ? '' : String(buffer);
  const events = [];
  const lastBreak = src.lastIndexOf('\n\n');
  if (lastBreak === -1) return { events: events, rest: src };
  const complete = src.slice(0, lastBreak + 2);
  const rest = src.slice(lastBreak + 2);
  for (const frame of complete.split('\n\n')) {
    if (!frame.trim()) continue;
    let type = '';
    let dataLine = null;
    for (const line of frame.split('\n')) {
      if (line.indexOf('event:') === 0) type = line.slice(6).trim();
      else if (line.indexOf('data:') === 0) dataLine = line.slice(5).trim();
    }
    if (dataLine == null) continue;
    let data;
    try {
      data = JSON.parse(dataLine);
    } catch (err) {
      continue; // malformed payload: skip the frame, never throw
    }
    events.push({ type: type, data: _commsNormalizeSseData(type, data) });
  }
  return { events: events, rest: rest };
}

// POST the message to /api/chat/stream, read the SSE body incrementally and
// call onEvent({type, data}) once per parsed frame, in order. Resolves to the
// `result` frame's data. Throws (so the caller falls back to the blocking
// path) when the response is not ok, when the body is not readable, when a
// read fails before the result arrived, or when the stream ends without a
// result event. A failure AFTER the result has been seen resolves with it
// instead, so the caller never re-renders a reply that already landed.
async function streamCommsMessage(message, onEvent) {
  const res = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: _commsRequestHeaders(),
    body: _commsRequestBody(message)
  });
  if (!res.ok) throw new Error('chat stream unavailable (non-2xx)');
  if (!res.body || typeof res.body.getReader !== 'function') {
    throw new Error('chat streaming unsupported (no readable response body)');
  }
  const decoder = new TextDecoder();
  const reader = res.body.getReader();
  let buffer = '';
  let result;
  let sawResult = false;
  for (;;) {
    let chunk;
    try {
      chunk = await reader.read();
    } catch (readErr) {
      if (sawResult) return result;
      throw readErr;
    }
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    const parsed = parseSseFrames(buffer);
    buffer = parsed.rest;
    for (const event of parsed.events) {
      if (event.type === 'result') {
        sawResult = true;
        result = event.data;
      }
      onEvent(_commsEventToWire(event));
    }
  }
  if (!sawResult) throw new Error('chat stream ended without a result event');
  return result;
}

async function sendCommsMessage(text) {
  const trimmed = text.trim();
  if (!trimmed) return;
  const userEl = appendCommsMessage('user', escapeHtml(trimmed));
  // The user bubble is appended BEFORE the fetch; if the fetch fails, this
  // turn is never pushed to commsHistory, so mark it as having no history
  // counterpart (the transcript reads its textContent without advancing the
  // per-role cursor). Cleared below once the turn is successfully recorded.
  if (userEl && userEl.dataset) userEl.dataset.noHistory = '1';
  const sendBtn = document.getElementById('comms-send');
  const onAir = document.getElementById('on-air');
  sendBtn.disabled = true;
  onAir.classList.add('live');
  const typingEl = _typingIndicatorEl();
  if (typingEl && typingEl.classList) typingEl.classList.remove('hidden');
  // Live tower bubble for this turn, created on the first streamed tool
  // event. Null when nothing streamed, in which case the reply is appended
  // exactly as the blocking path always did.
  let pending = null;
  let finalEl = null;
  const lastThreadChild = () => {
    const thread = document.getElementById('comms-thread');
    if (!thread || !thread.children || thread.children.length === 0) return null;
    return thread.children[thread.children.length - 1];
  };
  const renderPending = () => {
    if (!pending || !pending.el) return;
    const html = (pending.status ? _commsLiveStatusHtml(pending.status) : '')
      + renderToolTraceHtml(pending.calls);
    pending.el.className = 'msg tower';
    pending.el.innerHTML = _commsMessageInnerHtml('tower', html);
    const thread = document.getElementById('comms-thread');
    if (thread && typeof thread.appendChild === 'function') {
      // Real DOM: the bubble is already the last child, so this is a no-op
      // move. Harnesses snapshot outerHTML at append time, so re-appending
      // is what makes each incremental state observable.
      thread.appendChild(pending.el);
    }
    _scrollCommsToBottom();
  };
  const renderFinal = (reply, toolCalls) => {
    const hasError = Array.isArray(toolCalls) && toolCalls.some(c => c.result && c.result.error);
    const role = hasError ? 'tower denied' : 'tower';
    const bubbleHtml = renderMarkdown(reply) + renderToolTraceHtml(toolCalls);
    const target = (pending && pending.el) || finalEl;
    if (target) {
      target.className = `msg ${role}`;
      target.innerHTML = _commsMessageInnerHtml(role, bubbleHtml);
      const thread = document.getElementById('comms-thread');
      if (thread && typeof thread.appendChild === 'function') thread.appendChild(target);
      _scrollCommsToBottom();
    } else {
      finalEl = appendCommsMessage(role, bubbleHtml);
    }
  };
  const onStreamEvent = (event) => {
    if (!event || typeof event.type !== 'string') return;
    _hideTypingIndicator();
    if (event.type === 'tool_call') {
      const name = event.data && event.data.name != null ? String(event.data.name) : 'tool';
      if (!pending || !pending.el) {
        appendCommsMessage('tower', _commsLiveStatusHtml(name));
        pending = { el: lastThreadChild(), status: name, calls: [] };
        return;
      }
      pending.status = name;
      renderPending();
    } else if (event.type === 'tool_result') {
      if (!pending || !pending.el) {
        appendCommsMessage('tower', '');
        pending = { el: lastThreadChild(), status: '', calls: [] };
      }
      if (event.data && typeof event.data === 'object') pending.calls.push(event.data);
      renderPending();
    } else if (event.type === 'reply' || event.type === 'result') {
      const d = event.data && typeof event.data === 'object' ? event.data : {};
      const reply = d.reply != null ? d.reply : (d.text != null ? d.text : '');
      const calls = Array.isArray(d.tool_calls) ? d.tool_calls : (pending && pending.calls) || [];
      renderFinal(reply, calls);
    }
  };
  try {
    let data = null;
    if (_commsStreamingSupported()) {
      try {
        data = await streamCommsMessage(trimmed, onStreamEvent);
      } catch (streamErr) {
        data = null; // any stream failure falls back to the blocking path
      }
    }
    if (!data) {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: _commsRequestHeaders(),
        body: _commsRequestBody(trimmed)
      });
      if (!res.ok) throw new Error('non-2xx');
      data = await res.json();
      renderFinal(data.reply, data.tool_calls);
    }
    commsHistory.push({ role: 'user', content: trimmed });
    commsHistory.push({ role: 'assistant', content: data.reply });
    // The turn is recorded now, so the user bubble appended before the fetch
    // has a history counterpart again.
    if (userEl && userEl.dataset) delete userEl.dataset.noHistory;
  } catch (e) {
    const catchHtml = escapeHtml("Couldn't reach the tower - try again.");
    let deniedEl = null;
    if (pending && pending.el) {
      deniedEl = pending.el;
      deniedEl.className = 'msg tower denied';
      deniedEl.innerHTML = _commsMessageInnerHtml('tower denied', catchHtml);
      const thread = document.getElementById('comms-thread');
      if (thread && typeof thread.appendChild === 'function') thread.appendChild(deniedEl);
      _scrollCommsToBottom();
    } else {
      deniedEl = appendCommsMessage('tower denied', catchHtml);
    }
    // Neither this bubble nor the user bubble appended before the fetch is
    // recorded in commsHistory, so mark both: the transcript reads their
    // textContent without advancing the per-role cursor.
    if (deniedEl && deniedEl.dataset) deniedEl.dataset.noHistory = '1';
    if (userEl && userEl.dataset) userEl.dataset.noHistory = '1';
  } finally {
    sendBtn.disabled = false;
    onAir.classList.remove('live');
    pending = null;
    if (_typingEl && _typingEl.classList) _typingEl.classList.add('hidden');
  }
}

async function populateIngestPlanOptions() {
  const select = document.getElementById('ingest-plan-name');
  if (!select) return;
  try {
    const data = await fetchIngestablePlans();
    const plans = Array.isArray(data && data.plans) ? data.plans : [];
    if (plans.length === 0) {
      select.innerHTML = '<option value="" disabled selected>no plans found</option>';
      return;
    }
    const options = ['<option value="" disabled selected>select a plan\u2026</option>'].concat(
      plans.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`)
    );
    select.innerHTML = options.join('');
  } catch (err) {
    select.innerHTML = '<option value="" disabled selected>failed to load plans</option>';
  }
}

// Wire UI events
const commsSendBtn = document.getElementById('comms-send');
if (commsSendBtn) {
  commsSendBtn.addEventListener('click', () => {
    const input = document.getElementById('comms-input');
    sendCommsMessage(input.value);
    input.value = '';
  });
}
const commsInput = document.getElementById('comms-input');
if (commsInput) {
  commsInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendCommsMessage(commsInput.value);
      commsInput.value = '';
    }
  });
}
const commsChips = typeof document.querySelectorAll === 'function'
  ? document.querySelectorAll('.comms-chip')
  : [];
commsChips.forEach((chip) => {
  chip.addEventListener('click', () => {
    const message = chip.dataset.commsChipMessage || chip.textContent;
    sendCommsMessage(message);
  });
});
const traceToggleButton = document.getElementById('comms-trace-toggle');
if (traceToggleButton) {
  traceToggleButton.addEventListener('click', () => {
    showTrace = !showTrace;
    try {
      localStorage.setItem('commsShowTrace', showTrace ? 'true' : 'false');
    } catch (err) {
      // storage unavailable; visibility still applies
    }
    applyTraceVisibility();
  });
}

const commsResetBtn = document.getElementById('comms-reset');
if (commsResetBtn) commsResetBtn.addEventListener('click', resetCommsThread);
const commsExportBtn = document.getElementById('comms-export');
const ingestPlanNameInput = document.getElementById('ingest-plan-name');
const ingestPlanSubmitButton = document.getElementById('ingest-plan-submit');
const ingestPlanStatus = document.getElementById('ingest-plan-status');
if (commsExportBtn) commsExportBtn.addEventListener('click', exportCommsThread);

applyTraceVisibility();
populateIngestPlanOptions();

// Ingest a saved plan from the Comms panel (CIH-4). The plan name travels in
// the URL via ingestPlan(); the status element is the only surface this
// writes to, and every value derived from user input or the server goes
// through renderIngestStatusHtml's escaping. Never throws: a failed request
// leaves the Comms view usable.
async function submitIngestPlan() {
  const name = normalizePlanName(ingestPlanNameInput.value);
  if (!name) {
    ingestPlanStatus.textContent = 'enter a plan name';
    return;
  }
  ingestPlanStatus.textContent = 'Ingesting…';
  try {
    const result = await ingestPlan(name);
    ingestPlanStatus.innerHTML = renderIngestStatusHtml({ ok: true, planName: name, result });
  } catch (err) {
    ingestPlanStatus.innerHTML = renderIngestStatusHtml({
      ok: false,
      status: err && err.status,
      detail: err && err.message,
    });
  }
}

if (ingestPlanSubmitButton) {
  ingestPlanSubmitButton.addEventListener('click', submitIngestPlan);
}
if (ingestPlanNameInput) {
  ingestPlanNameInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') submitIngestPlan();
  });
}

async function updateCommsSubtitle() {
  const sub = document.getElementById('comms-sub');
  if (!sub) return;
  try {
    const cfg = await fetchJson('/api/config');
    const roles = Array.isArray(cfg.roles) ? cfg.roles : [];
    const chatRole = roles.find((r) => r.role === 'chat');
    if (chatRole && chatRole.provider && chatRole.model) {
      sub.textContent = `chat -> ${chatRole.provider}/${chatRole.model}`.replace('->', String.fromCharCode(0x2192));
    } else {
      sub.textContent = 'chat';
    }
  } catch (e) {
    sub.textContent = 'chat';
  }
}

export { renderToolTraceHtml, appendCommsMessage, sendCommsMessage, updateCommsSubtitle, resetCommsThread, parseSseFrames, streamCommsMessage };