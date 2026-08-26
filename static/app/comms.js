import { state } from "./state.js";
import { escapeHtml } from "./render/board.js";

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

function appendCommsMessage(role, html) {
  const thread = document.getElementById('comms-thread');
  const landing = document.getElementById('comms-landing');
  const el = document.createElement('div');
  el.className = `msg ${role}`;
  el.innerHTML = html;
  const first = thread.children.length === 0;
  thread.appendChild(el);
  if (first) {
    landing.style.display = 'none';
    thread.style.display = 'flex';
  }
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
      body: JSON.stringify({ plan_name: state.selectedPlan, message: trimmed, history: null })
    });
    if (!res.ok) throw new Error('non-2xx');
    const data = await res.json();
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

export { renderToolTraceHtml, appendCommsMessage, sendCommsMessage };
