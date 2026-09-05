import { state } from "../state.js";
import { escapeHtml } from "./board.js";
import {
  renderCopyButton, copyValues, loadStoryJournal, loadStoryChecklist,
} from "./plan-detail.js";

// story-modal.js cannot statically `import ... from "../../app.js"`: app.js's
// dynamic-import test harness cache-busts its own URL with a `?t=` query
// param, so a static back-import here would resolve to a SECOND, separate
// app.js module instance and re-run its top-level init out of order (see the
// identical comment in plan-detail.js/plan-list.js, where this was first
// reproduced). app.js instead calls initStoryModal() once at init with its
// own function reference and the NOTIF_SEVERITY_COLOR constant (kept declared
// in app.js so a static-source assertion in the test suite can still find
// it there), breaking the import-graph cycle while keeping identical
// call-time behavior.
let _backendErrorEl;
import { NOTIF_SEVERITY_COLOR } from "./notifications.js";
function initStoryModal({ backendErrorEl, notifSeverityColor }) {
  _backendErrorEl = backendErrorEl;
}

// True when a value should be rendered in a monospace <pre> block instead
// of an inline <dd>. Long paths, IDs, URLs, commit hashes, and JSON-ish
// blobs benefit from a fixed-width wrap so users can read/copy them.
function isLongValue(value) {
  const s = String(value == null ? "" : value);
  if (s.length > 60) return true;
  return /[\\/\n]/.test(s);
}

function filterStoryNotifications(records, storyKey) {
  if (!records) return [];
  return records.filter((r) => r && r.story_key === storyKey);
}

function renderStoryModalNotifications(records) {
  if (!records || !records.length) {
    return '<p class="modal-empty" data-notifications-empty>No notifications for this story.</p>';
  }
  return records.slice().reverse().map((r) => {
    const severity = (r && r.severity) || "info";
    const color = NOTIF_SEVERITY_COLOR[severity] || NOTIF_SEVERITY_COLOR.info;
    const countBadge = r && r.count > 1
      ? ` <span class="badge">x${escapeHtml(String(r.count))}</span>`
      : "";
    return `<div class="log-line">`
      + `<span class="badge" style="--badge-color: var(${color})">${escapeHtml(severity)}</span>`
      + countBadge
      + ` ${escapeHtml((r && r.message) || "")}</div>`;
  }).join("");
}

function showStoryModal(planName, story, key, notificationRecords) {
  _renderStoryModalBody(planName, story, key, notificationRecords);
}

function _renderStoryModalBody(planName, story, key, notificationRecords) {
  const modal = document.getElementById("story-modal");
  const body = document.getElementById("story-modal-body");
  const deps = story.dependencies;
  const depsText = Array.isArray(deps) ? deps.join(", ") : "";
  const depsShow = Array.isArray(deps) && deps.length > 0 ? depsText : null;
  const storyNotifications = filterStoryNotifications(notificationRecords, key);
  body.innerHTML = storyNotifications;
  // Track which plan/story the modal is currently showing so the async
  // log fetch can resolve into the right slot and so a stale fetch that
  // resolves after the user opened a different story can't write into the
  // wrong modal.
  modal.dataset.plan = state.selectedPlan || "";
  modal.dataset.story = key;

  // Field lists per section. Each entry: [label, value, opts].
  //   - `opts.copy` enables the click-to-copy button on that value, using
  //     `opts.copyField` (or fall back to label) as the data-copy key.
  //   - `opts.mono` forces monospace <pre> rendering for short but
  //     structured values (e.g. a 12-char commit hash).
  //
  // The four sections match the documented field grouping in the story:
  //   - Identity: who/what the story is
  //   - Lifecycle: where the agent is in execution
  //   - Dispatch & review: agent-loop bookkeeping and reviewer notes
  //   - Errors: failure/pause context surfaced to humans
  const identity = [
    ["Key", key, { copy: true, copyField: "key" }],
    ["Summary", story.summary],
    ["Persona", story.persona],
    ["Model", story.dispatched_model || story.model],
    ["Risk", story.risk],
    ["Dependencies", depsShow],
  ];
  const lifecycle = [
    ["Status", story.status],
    ["Backend", story.backend],
    ["Escalated", story.escalated],
    ["Worktree", story.worktree, { copy: true, copyField: "worktree" }],
    ["Branch", story.branch],
    ["PID", story.pid, { copy: true, copyField: "pid", mono: true }],
    ["PR URL", story.pr_url, { copy: true, copyField: "pr_url", mono: true }],
    ["Last commit", story.last_commit, { mono: true }],
    ["Interrupted at", story.interrupted_at],
  ];
  const dispatch = [
    ["Dispatch attempts", story.dispatch_attempts],
    ["Rework attempts", story.rework_attempts],
    ["Merge attempts", story.merge_attempts],
    ["Review verdict", story.review_verdict],
    ["Review feedback", story.review_feedback],
  ];
  const errors = [
    ["Dispatch error", story.dispatch_error],
    ["Merge error", story.merge_error],
    ["Failure reason", story.failure_reason],
    ["Parked reason", story.parked_reason],
  ];

  // Reset any previously-cached copy values before populating this story.
  // Stale entries from a prior open would otherwise let a stale field
  // name resolve to its old value, which would be both confusing and a
  // potential information leak across stories.
  copyValues.clear();

  // Render a field row, applying long/mono/copy semantics.
  function renderRow(label, value, opts) {
    opts = opts || {};
    const v = value;
    if (v === undefined || v === null || v === "") return "";
    const mono = opts.mono || isLongValue(v);
    const inner = mono
      ? `<pre class="mono">${escapeHtml(String(v))}</pre>`
      : escapeHtml(v);
    const btn = opts.copy ? renderCopyButton(opts.copyField || label, v) : "";
    return `<dt>${escapeHtml(label)}</dt><dd>${inner}${btn}</dd>`;
  }

  function renderSection(title, rows) {
    const html = rows.map(([label, value, opts]) => renderRow(label, value, opts))
      .filter(Boolean)
      .join("");
    if (!html) return "";
    // Section titles are hardcoded literal strings, not user input — emit
    // them raw so "&" survives as "&" rather than "&amp;" (which would
    // still render correctly in the browser but makes the markup noisy).
    return `<h3 class="modal-section">${title}</h3><dl>${html}</dl>`;
  }

  const sections = [
    renderSection("Identity", identity),
    renderSection("Lifecycle", lifecycle),
    renderSection("Dispatch & review", dispatch),
    renderSection("Errors", errors),
  ].filter(Boolean).join("");

  body.innerHTML = `
    <h2 class="modal-title">${escapeHtml(key)}</h2>
    ${sections || "<p class=\"modal-empty\">No fields to display.</p>"}
    <div data-checklist-slot>
      <h3 class="modal-section">Checklist</h3>
      <p class="modal-empty" data-checklist-empty>No checklist yet.</p>
    </div>
    <div data-journal-slot>
      <h3 class="modal-section">Journal</h3>
      <p class="modal-empty" data-journal-empty>No journal yet.</p>
    </div>
    <div data-notifications-slot>
      <h3 class="modal-section">Notifications</h3>
      ${renderStoryModalNotifications(storyNotifications)}
    </div>
    <section class="dsh-modal-log-section" aria-labelledby="dsh-log-heading">
      <h3 id="dsh-log-heading" class="modal-section">Log</h3>
      <div class="dsh-modal-log-status" data-state="loading">
        Loading log&hellip;
      </div>
      <pre class="dsh-modal-log mono hidden" tabindex="0"
           aria-label="Story log tail"></pre>
      <p class="dsh-modal-log-empty hidden">No log available.</p>
    </section>
  `;
  modal.classList.remove("hidden");

  // Reflect the story's current backend in the per-story backend selector so
  // the user sees the resolved value before editing it. If the story's backend
  // is not one of the static <option> values, add it dynamically so the select
  // shows the actual resolved value instead of silently falling back to the
  // first option.
  const backendSelect = document.getElementById("backend-select");
  if (backendSelect && story && story.backend) {
    const backend = String(story.backend);
    const hasOption = Array.from(backendSelect.options).some(
      (o) => o.value === backend
    );
    if (!hasOption) {
      const opt = document.createElement("option");
      opt.value = backend;
      opt.textContent = backend;
      backendSelect.appendChild(opt);
    }
    backendSelect.value = backend;
  }
  const backendError = _backendErrorEl();
  if (backendError) backendError.textContent = "";

  // Fire-and-forget journal fetch. The empty-state placeholder is already
  // in the DOM so the modal opens immediately; the slot is updated by
  // loadStoryJournal once the response arrives (or stays as the empty
  // state on error / unavailable). The race guard in loadStoryJournal
  // ensures a slow prior fetch can't clobber a newly-opened story.
  if (planName) loadStoryJournal(planName, key, body);

  // Same fire-and-forget pattern for the worktree checklist + scratchpad
  // (Tier 0 progress view). Most stories have no checklist (not run under
  // PIPELINE_DECOMPOSE), so the slot usually stays at the empty state.
  if (planName) loadStoryChecklist(planName, key, body);

  // Kick off the tail fetch now that the modal is shown. The fetch
  // resolves into the placeholder by class — if the user opens a different
  // story before the fetch lands, we walk away (loadStoryLog guards on
  // modal.dataset.plan/story still matching).
  loadStoryLog();
}

// Asynchronously load /api/plans/{plan}/stories/{key}/log into the Log
// section of the currently-open modal.
//
// Why this is async instead of inlined: the log file can be tens of KB
// and we don't want to block the modal rendering on it. We render a
// "Loading log…" placeholder synchronously, then swap in the tail (or
// the empty state) when the fetch resolves.
//
// Resilience contract (matches the backend): any fetch error or
// non-200 response renders the empty state. The endpoint itself never
// returns 500 for "log gone", so in practice the error path here is
// only reachable if the network fails; we surface "No log available"
// in both cases rather than a misleading error message, because the
// user's question is "is there a log" and the answer is the same.
//
// We swallow JSON/parse failures silently — a partial response from a
// flaky proxy should not blank the modal.
async function loadStoryLog() {
  const modal = document.getElementById("story-modal");
  const plan = modal.dataset.plan;
  const key = modal.dataset.story;
  if (!plan || !key) return;

  const section = modal.querySelector(".dsh-modal-log-section");
  const statusEl = modal.querySelector(".dsh-modal-log-status");
  const tailEl = modal.querySelector(".dsh-modal-log");
  const emptyEl = modal.querySelector(".dsh-modal-log-empty");
  if (!section || !statusEl || !tailEl || !emptyEl) return;

  const url = `/api/plans/${encodeURIComponent(plan)}`
    + `/stories/${encodeURIComponent(key)}/log?lines=200`;

  let body;
  try {
    const res = await fetch(url);
    if (!res.ok) {
      // Endpoint gone or the plan/story was renamed between modal open
      // and fetch: render empty state, don't raise.
      body = { available: false, lines: [] };
    } else {
      body = await res.json();
    }
  } catch {
    body = { available: false, lines: [] };
  }

  // Stale response guard: if the user closed or navigated the modal
  // (or opened another story) before this fetch resolved, do nothing.
  if (modal.dataset.plan !== plan || modal.dataset.story !== key) return;
  if (modal.classList.contains("hidden")) return;

  const isAvailable = !!(body && body.available);
  const lines = (body && Array.isArray(body.lines)) ? body.lines : [];

  if (!isAvailable || lines.length === 0) {
    // available:false OR available:true but the file was zero bytes:
    // both render the same "No log available" empty state. The user
    // asked "is there a log" and the answer is no.
    statusEl.classList.add("hidden");
    tailEl.classList.add("hidden");
    emptyEl.classList.remove("hidden");
    section.dataset.state = "empty";
    return;
  }

  // Escape per-line so binary garbage / &<> / replacement chars render
  // as text instead of breaking the page. Newline preserved by the
  // surrounding <pre>.
  const text = lines.map((l) => escapeHtml(String(l))).join("\n");
  tailEl.textContent = text;
  statusEl.classList.add("hidden");
  emptyEl.classList.add("hidden");
  tailEl.classList.remove("hidden");
  section.dataset.state = "ready";
}

function hideStoryModal() {
  const modal = document.getElementById("story-modal");
  // Invalidate any in-flight log fetch so it can't write into a hidden
  // modal that the user just closed.
  delete modal.dataset.plan;
  delete modal.dataset.story;
  modal.classList.add("hidden");
}

export {
  initStoryModal, filterStoryNotifications,
  renderStoryModalNotifications, showStoryModal, _renderStoryModalBody,
  hideStoryModal,
};
