import { state } from "./state.js";
import { escapeHtml } from "./render/board.js";
import { fetchJson } from "./api.js";
let commsHistory = []

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

function appendCommsMessage(role, html) {
  const thread = document.getElementById('comms-thread');
  const landing = document.getElementById('comms-landing');
  const el = document.createElement('div');
  el.className = `msg ${role}`;
  const isTower = role.split(' ')[0] === 'tower';
  const dot = isTower ? '<span class="who-dot" aria-hidden="true"></span>' : '';
  const flag = role.indexOf('denied') !== -1 ? ' <span class="who-flag">DENIED</span>' : '';
  const who = `<span class="who">${dot}${_commsRoleLabel(role)}${flag}<span class="who-time">${_commsTimeLabel(new Date())}</span></span>`;
  el.innerHTML = `${who}<div class="bubble">${html}</div>`;
  const first = thread.children.length === 0;
  thread.appendChild(el);
  if (first) {
    landing.style.display = 'none';
    thread.style.display = 'flex';
  }
  _scrollCommsToBottom();
}

function _commsTranscriptMarkdown() {
  const thread = document.getElementById('comms-thread');
  const lines = ['# Tower transcript', ''];
  if (thread && typeof thread.querySelectorAll === 'function') {
    thread.querySelectorAll('.msg').forEach((node) => {
      const label = node.className.indexOf('user') !== -1 ? 'Ground' : 'Tower';
      const bubble = typeof node.querySelector === 'function' ? node.querySelector('.bubble') : null;
      const text = (bubble ? bubble.textContent : node.textContent || '').trim();
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

async function sendCommsMessage(text) {
  const trimmed = text.trim();
  if (!trimmed) return;
  appendCommsMessage('user', escapeHtml(trimmed));
  const sendBtn = document.getElementById('comms-send');
  const onAir = document.getElementById('on-air');
  sendBtn.disabled = true;
  onAir.classList.add('live');
  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plan_name: state.selectedPlan, message: trimmed, history: commsHistory })
    });
    if (!res.ok) throw new Error('non-2xx');
    const data = await res.json();
    commsHistory.push({ role: 'user', content: trimmed });
    commsHistory.push({ role: 'assistant', content: data.reply });
    const hasError = Array.isArray(data.tool_calls) && data.tool_calls.some(c => c.result && c.result.error);
    const role = hasError ? 'tower denied' : 'tower';
    const bubbleHtml = escapeHtml(data.reply) + renderToolTraceHtml(data.tool_calls);
    appendCommsMessage(role, bubbleHtml);
  } catch (e) {
    appendCommsMessage('tower denied', escapeHtml("Couldn't reach the tower - try again."));
  } finally {
    sendBtn.disabled = false;
    onAir.classList.remove('live');
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
const commsResetBtn = document.getElementById('comms-reset');
if (commsResetBtn) commsResetBtn.addEventListener('click', resetCommsThread);
const commsExportBtn = document.getElementById('comms-export');
if (commsExportBtn) commsExportBtn.addEventListener('click', exportCommsThread);

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

export { renderToolTraceHtml, appendCommsMessage, sendCommsMessage, updateCommsSubtitle };
