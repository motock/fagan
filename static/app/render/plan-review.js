// Plan review panel: the approval record and, while a plan is pending, the
// Approve / Request-changes controls.
//
// renderPlanReview fetches GET /api/plans/<plan>/review and builds the panel
// into `container`. Every decision POSTs and then refetches the review and
// re-renders from that payload; the screen is never updated optimistically.
// A decision sends the current_digest of the payload on screen when its
// control was built, never a digest kept from an earlier fetch.
//
// Built with DOM APIs and textContent only: plan names, story summaries,
// warnings and reasons must never be parsed as markup. Error text is fixed
// per status; the server's response body is never shown.

import { fetchJson, postJsonBody } from "../api.js";

const REASON_MAX = 2000;

const MESSAGE_FORBIDDEN =
  "Sign in with your organisation account (or you are not allowed to decide this plan).";
const MESSAGE_CONFLICT = "This plan changed or was already decided. Reload and review it again.";
const MESSAGE_GONE = "This plan no longer exists.";
const MESSAGE_DECIDE_GENERIC = "Could not record the decision. Try again.";
const MESSAGE_LOAD_GENERIC = "Could not load the plan review. Try again.";
const MESSAGE_APPROVE_BLOCKED =
  "The plan changed since it was submitted. Reload to review the current version.";

// api.js throws `<url> -> <status>` for non-OK responses. A network failure
// is a TypeError with no status, which reports as null.
function _statusFromError(err) {
  const match = /-> (\d{3})$/.exec(String((err && err.message) || ""));
  return match ? Number(match[1]) : null;
}

function _decisionMessage(status) {
  if (status === 403) return MESSAGE_FORBIDDEN;
  if (status === 404) return MESSAGE_GONE;
  if (status === 409) return MESSAGE_CONFLICT;
  return MESSAGE_DECIDE_GENERIC;
}

// Plans whose decision request is in flight, keyed by plan name. Checked
// before any await so a second click on the same plan is dropped.
const _pending = new Set();

function resetPlanReviewState() {
  _pending.clear();
}

function _reviewUrl(planName) {
  return `/api/plans/${encodeURIComponent(planName)}/review`;
}

function _text(value) {
  return value === null || value === undefined ? "" : String(value);
}

function _add(parent, tag, text, dataset) {
  const el = document.createElement(tag);
  if (text) el.textContent = text;
  for (const [key, value] of Object.entries(dataset || {})) el.dataset[key] = value;
  parent.appendChild(el);
  return el;
}

// Reason length in code points, matching the server's character count.
function _codePoints(value) {
  return [...value].length;
}

function _renderMessage(container, text) {
  container.textContent = "";
  const p = _add(container, "p", text, { reviewField: "message" });
  p.setAttribute("role", "status");
}

// Fetch the review and build the panel. `message` is shown above it after a
// decision. A failed fetch replaces the panel with a fixed message instead.
async function _load(container, planName, message) {
  let payload;
  try {
    payload = await fetchJson(_reviewUrl(planName));
  } catch (err) {
    const status = _statusFromError(err);
    _renderMessage(container, status === 404 ? MESSAGE_GONE : MESSAGE_LOAD_GENERIC);
    return;
  }
  _render(container, planName, payload, message);
}

async function renderPlanReview(container, planName) {
  await _load(container, planName, "");
}

function _render(container, planName, payload, message) {
  container.textContent = "";
  const view = { container, planName, payload, controls: [] };
  const approval = payload.approval || {};
  const status = approval.status;

  _add(container, "h3", "Plan review");
  _add(container, "p", `Approval status: ${_text(status) || "no approval record"}`);
  if (approval.self_approved === true) {
    _add(container, "p", "This plan was approved by its own author.");
  }
  if (payload.viewer_is_author === true) {
    _add(container, "p", "You ingested this plan.");
  }
  _add(container, "p", `Owner: ${_text(payload.owner) || "none"}`);
  _add(container, "p", `Team: ${_text(payload.team) || "none"}`);

  if (Array.isArray(payload.warnings) && payload.warnings.length) {
    const list = _add(container, "ul", "", { reviewField: "warnings" });
    for (const warning of payload.warnings) _add(list, "li", _text(warning));
  }

  const table = _add(container, "table", "", { reviewField: "stories" });
  for (const story of payload.stories || []) {
    const row = _add(table, "tr", "", { reviewRow: "story" });
    _add(row, "td", _text(story.key));
    _add(row, "td", _text(story.summary));
    _add(row, "td", _text(story.risk));
    _add(row, "td", _text(story.persona));
    _add(row, "td", Array.isArray(story.files) ? String(story.files.length) : "undeclared");
  }

  if (status === "pending_approval") _buildDecisionControls(view);
  if (message) _add(container, "p", message, { reviewField: "message" }).setAttribute("role", "status");
}

function _buildDecisionControls(view) {
  const { container, payload } = view;
  const approve = _add(container, "button", "Approve", { reviewAction: "approve" });
  approve.setAttribute("type", "button");
  approve.disabled = payload.digest_current !== true;
  if (approve.disabled) _add(container, "p", MESSAGE_APPROVE_BLOCKED);
  approve.addEventListener("click", () => _approve(view));

  const box = _add(container, "textarea", "", { reviewField: "reason" });
  const counter = _add(container, "p", "", { reviewField: "counter" });
  const request = _add(container, "button", "Request changes", { reviewAction: "request-changes" });
  request.setAttribute("type", "button");
  box.addEventListener("input", () => _syncReason(box, counter, request));
  request.addEventListener("click", () => _requestChanges(view, box));
  _syncReason(box, counter, request);

  view.controls = [approve, box, request];
}

// Counter shows the stripped length; submit is enabled only for 1..REASON_MAX.
function _syncReason(box, counter, request) {
  const length = _codePoints(box.value.trim());
  counter.textContent = `${length} / ${REASON_MAX} characters`;
  request.disabled = length < 1 || length > REASON_MAX;
}

function _approve(view) {
  if (!view.payload.digest_current) return undefined;
  return _decide(view, "approve", { digest: view.payload.current_digest });
}

function _requestChanges(view, box) {
  const reason = box.value.trim();
  const length = _codePoints(reason);
  if (length < 1 || length > REASON_MAX) return undefined;
  return _decide(view, "request-changes", {
    digest: view.payload.current_digest,
    reason,
  });
}

async function _decide(view, action, body) {
  const { container, planName } = view;
  if (_pending.has(planName)) return;
  _pending.add(planName);
  for (const control of view.controls) control.disabled = true;

  let message = "";
  try {
    await postJsonBody(`/api/plans/${encodeURIComponent(planName)}/review/${action}`, body);
  } catch (err) {
    message = _decisionMessage(_statusFromError(err));
  }
  try {
    // Refetch whether the decision succeeded or not: the screen shows what
    // the server now says, not what this click expected.
    await _load(container, planName, message);
  } finally {
    _pending.delete(planName);
  }
}

export { renderPlanReview, resetPlanReviewState };
