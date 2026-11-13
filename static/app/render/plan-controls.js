// Pause / resume control for the plan detail header.
//
// togglePlanPaused POSTs to the existing /pause or /resume endpoint and, on
// success, asks the injected refresh (main.js's refresh(), the same one the
// plan-list archive toggle uses) to refetch. It never patches the DOM itself.
//
// The refresh is injected through initPlanControls rather than imported:
// main.js owns refresh() and an import back into it would form a module cycle
// (see the note in plan-detail.js).

import { postJson } from "../api.js";
import { escapeHtml } from "./board.js";

let _refresh = () => {};
function initPlanControls({ refresh }) {
  _refresh = refresh;
}

// Fixed copy only. Server text is never shown: a 403 or 500 body may carry
// internal detail, so the control reports the status class and nothing else.
const MESSAGE_FORBIDDEN = "You are not permitted to pause or resume this plan.";
const MESSAGE_GONE = "This plan no longer exists.";
const MESSAGE_GENERIC = "Could not change the plan. Try again.";

// api.js throws `<url> -> <status>` for non-OK responses. A network failure
// is a TypeError with no status, which reports as null.
function _statusFromError(err) {
  const match = /-> (\d{3})$/.exec(String((err && err.message) || ""));
  return match ? Number(match[1]) : null;
}

// Plans currently waiting on a pause/resume response, keyed by plan name.
// Survives poll re-renders so a rebuilt button stays disabled mid-request.
const _pending = new Set();
// Last failure message per plan, so a poll re-render keeps showing it.
const _messages = new Map();
// Most recent plan object mounted per name; a repaint after a request reads it.
const _lastPlan = new Map();

function resetPlanControlsState() {
  _pending.clear();
  _messages.clear();
  _lastPlan.clear();
}

async function togglePlanPaused(planName, currentlyPaused) {
  const action = currentlyPaused ? "resume" : "pause";
  try {
    await postJson(`/api/plans/${encodeURIComponent(planName)}/${action}`);
  } catch (err) {
    return { ok: false, status: _statusFromError(err) };
  }
  await _refresh();
  return { ok: true };
}

function _messageFor(status) {
  if (status === 403) return MESSAGE_FORBIDDEN;
  if (status === 404) return MESSAGE_GONE;
  return MESSAGE_GENERIC;
}

// Only approved plans can run, so pausing one that is not approved is
// meaningless. A paused plan always shows Resume, so it can be un-paused even
// if its approval has since lapsed.
function _showsControl(plan) {
  return plan.paused === true || (plan.approval && plan.approval.status === "approved");
}

// Re-render the control into every mounted slot for this plan. Used after a
// request settles, because the refresh that follows a success has already
// replaced the slot the click came from.
function _repaint(planName) {
  const plan = _lastPlan.get(planName);
  if (!plan) return;
  for (const slot of document.querySelectorAll("[data-plan-control-slot]")) {
    if (slot.dataset.planName === planName) mountPlanControls(slot, plan);
  }
}

async function _onClick(planName, currentlyPaused, button) {
  if (_pending.has(planName)) return;
  _pending.add(planName);
  _messages.delete(planName);
  button.disabled = true;

  const result = await togglePlanPaused(planName, currentlyPaused);

  _pending.delete(planName);
  if (!result.ok) _messages.set(planName, _messageFor(result.status));
  _repaint(planName);
}

// Build the control into an empty slot element with DOM APIs only. Plan names
// and server text never pass through innerHTML.
function mountPlanControls(slot, plan) {
  if (!slot) return;
  _lastPlan.set(plan.name, plan);
  slot.textContent = "";
  if (!_showsControl(plan)) return;

  const paused = plan.paused === true;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "plan-control-btn";
  button.textContent = paused ? "Resume plan" : "Pause plan";
  button.disabled = _pending.has(plan.name);
  button.addEventListener("click", () => _onClick(plan.name, paused, button));
  slot.appendChild(button);

  const message = _messages.get(plan.name);
  if (message) {
    const span = document.createElement("span");
    span.className = "plan-control-msg";
    span.setAttribute("role", "status");
    span.textContent = message;
    slot.appendChild(span);
  }
}

// Header markup for the slot. Only the escaped plan name is interpolated, and
// only as a data attribute so _repaint can find the slot again.
function planControlSlotHtml(plan) {
  return `<span class="plan-control-slot" data-plan-control-slot data-plan-name="${escapeHtml(plan.name)}"></span>`;
}

export {
  togglePlanPaused, initPlanControls, mountPlanControls,
  planControlSlotHtml, resetPlanControlsState,
};
