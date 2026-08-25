import {
  state, loadFilters, resetState,
  STATUS_COLUMNS, SORT_OPTIONS, VALID_SORTS, BACKEND_VALUES, ESCALATED_VALUES,
} from "./app/state.js";
import { fetchJson } from "./app/api.js";
import {
  HASH_KEYS, hashStateFrom, encodeHashState, parseHash, updateHash, clearHash, applyHashToState,
} from "./app/routing.js";
import {
  escapeHtml, chip, renderBoard,
} from "./app/render/board.js";
import {
  renderPlanList, _renderPlanListFull, resetPlanListState, initPlanList,
} from "./app/render/plan-list.js";
import {
  renderPlanDetail, capturePlanDetailState, restorePlanDetailState,
  setDiffNotificationsOnPoll, resetPlanDetailState, initPlanDetail, renderChecklist,
  loadStoryJournal, loadStoryChecklist, renderCopyButton, handleCopyClick, copyValues,
} from "./app/render/plan-detail.js";
import {
  initStoryModal, filterStoryNotifications,
  renderStoryModalNotifications, showStoryModal, _renderStoryModalBody,
  hideStoryModal,
} from "./app/render/story-modal.js";

const NOTIF_SEVERITY_COLOR = { "error": "--c-failed", "warning": "--c-parked", "info": "--c-unknown" };

// plan-list.js/plan-detail.js/story-modal.js can't statically import back
// from app.js (see their own comments on this) without breaking under the
// test harness's cache-busted app.js URL, so this wires their few
// app.js-level dependencies via injection instead. Safe to call immediately:
// selectPlan, selectComms, selectOverview, refresh, renderNotifications,
// renderDecisions, setNotifSeverityFilter, and _backendErrorEl are all
// hoisted function declarations, available before this line runs regardless
// of source order.
initPlanList({ selectPlan, selectComms, selectOverview, refresh });
initPlanDetail({ showStoryModal, renderNotifications, renderDecisions, setNotifSeverityFilter });
initStoryModal({ backendErrorEl: _backendErrorEl, notifSeverityColor: NOTIF_SEVERITY_COLOR });

// Toast state and helpers
let lastSeenNotificationByPlan = new Map();
let hasSeededNotifications = false;

// Pure function to pick new notifications
function pickNewNotifications(plans, seenMap) {
  const newNotifs = [];
  for (const plan of plans) {
    const rec = plan.latest_notification;
    if (!rec || rec.dedup_key == null) continue;
    const seen = seenMap.get(plan.name);
    if (seen !== rec.dedup_key) {
      newNotifs.push({ plan, record: rec });
    }
  }
  return newNotifs;
}

// Push a toast to stack
function pushToast({ severity, planName, storyKey, message }) {
  const stack = document.getElementById("toast-stack");
  if (!stack) return;
  const node = document.createElement("div");
  node.className = "toast";
  const stripe = NOTIF_SEVERITY_COLOR[severity] || NOTIF_SEVERITY_COLOR["info"];
  node.style.setProperty("--stripe", stripe);
  node.innerHTML = `
    <div class="toast-row">
      <span class="toast-key">${escapeHtml(planName)}</span>
      <span class="toast-msg">${escapeHtml(message)}</span>
      <button class="toast-dismiss" aria-label="Dismiss">✕</button>
    </div>
    <div class="toast-actions"></div>
  `;
  const dismiss = node.querySelector(".toast-dismiss");
  dismiss.addEventListener("click", () => node.remove());
  const actions = node.querySelector(".toast-actions");
  if (severity === "error" || severity === "warning") {
    const ask = document.createElement("button");
    ask.className = "toast-ask";
    ask.textContent = "Ask";
    ask.addEventListener("click", () => {
      selectComms();
      const input = document.getElementById("comms-input");
      if (input) {
        input.value = `${storyKey} ${severity === "error" ? "failed" : "is parked"} - what happened?`;
        input.focus();
      }
      node.remove();
    });
    actions.appendChild(ask);
  } else {
    setTimeout(() => node.remove(), 6000);
  }
  stack.appendChild(node);
}


let notifSeverityFilter = "all";
// plan-detail.js's renderPlanDetail can't reassign this module's `let`
// binding directly (ES module bindings are read-only to importers), so it
// calls this setter instead when the notif-severity filter chip is clicked.
function setNotifSeverityFilter(value) {
  notifSeverityFilter = value;
}
// Keyed-diff state for _diffOverviewPlanRows: maps each plan name to its
// live `.overview-plan-row` DOM node, mirroring plan-list.js's
// planListRowsByName. renderOverview rebuilds the rest of the section's
// markup (including a fresh empty `.overview-plan-list` <ul>) every poll
// tick, so this map - not the <ul>'s own children - is the only thing that
// lets a row survive across ticks: existing rows are moved (not recreated)
// into the new <ul>.
let overviewPlanRowsByName = new Map();

function filterNotifications(records, severity) {
  if (!records) return [];
  if (!severity || severity === "all") return records.slice();
  return records.filter((r) => (r && r.severity) === severity);
}

function decodeHtmlEntities(s) {
  return String(s).replace(/&(amp|lt|gt|quot|#39);/g, (m, name) => ({
    "amp": "&", "lt": "<", "gt": ">", "quot": '"', "#39": "'",
  }[name]));
}

function renderNotifications(records) {
  const chipRow = `<div class="filter-group"><span class="filter-group-label">Severity</span>${
    ["all", "error", "warning", "info"]
      .map((sev) => chip("notif-severity", sev, sev, notifSeverityFilter === sev))
      .join("")
  }</div>`;
  const filtered = filterNotifications(records, notifSeverityFilter);
  if (!filtered.length) {
    const emptyMsg = (records && records.length)
      ? "No notifications at this severity."
      : "No notifications yet.";
    return chipRow + `<p class="empty-state">${emptyMsg}</p>`;
  }
  return chipRow + filtered.reverse().map(function (r) {
    var color = NOTIF_SEVERITY_COLOR[r.severity] || "--c-unknown";
    var sev = escapeHtml(r.severity || "");
    var parts = [];
    parts.push('<div class="log-line" data-dedup-key="' + escapeHtml(r.dedup_key || (r.ts + '-' + r.message)) + '">');
    parts.push('<span class="badge" style="--badge-color: var(' + color + ')">' + sev + '</span>');
    if (r.story_key) {
      parts.push('<span class="mono">' + escapeHtml(r.story_key) + '</span>');
    }
    parts.push(escapeHtml(r.message || ""));
    if (r.count && r.count > 1) {
      parts.push('<span class="badge">x' + escapeHtml(String(r.count)) + '</span>');
    }
    if (r.ts) {
      parts.push(escapeHtml(r.ts));
    }
    parts.push('</div>');
    return parts.join(' ');
  }).join('');
}

// Incrementally append new notification rows based on dedup_key
function _diffNotificationsPanel(panelBodyEl, records) {
  if (!panelBodyEl || !records) return;
  const existing = new Set(
    Array.from(panelBodyEl.querySelectorAll('[data-dedup-key]')).map(
      (el) => decodeHtmlEntities(el.getAttribute('data-dedup-key'))
    )
  );
  for (const r of records) {
    const key = r.dedup_key || (r.ts + '-' + r.message);
    if (existing.has(key)) continue;
    const tmp = document.createElement('div');
    tmp.innerHTML = renderNotifications([r]);
    const node = tmp.querySelector('.log-line');
    if (node) panelBodyEl.appendChild(node);
  }
}

function renderDecisions(decisions) {
  if (!decisions.length) return '<p class="empty-state">No decisions logged yet.</p>';
  return decisions.slice().reverse().map((d) => `
    <div class="decision">
      <div class="decision-q">${escapeHtml(d.question)}</div>
      <div class="decision-meta">
        ${escapeHtml(d.ruling || "")} — tier=${escapeHtml(d.tier || "?")}, risk=${escapeHtml(d.risk || "?")}
        · ${escapeHtml(d.decided_at || "")}
      </div>
    </div>
  `).join("");
}


// Flash the header refresh indicator. Called once per successful refresh so
// the user sees liveness without staring at the clock. The .flashing class
// drives the dot's pulse animation; we toggle it off after the animation so
// the element returns to its idle (invisible) state.
function flashRefreshIndicator() {
  const el = document.getElementById("refresh-indicator");
  if (!el) return;
  // Re-render contents each time so the animation restarts cleanly even on
  // back-to-back flashes that would otherwise be coalesced by the browser.
  el.innerHTML = '<span class="dot" aria-hidden="true"></span>'
    + '<span class="refresh-indicator-label">updating\u2026</span>';
  // Force a reflow so removing + re-adding the class restarts the keyframes
  // when refreshes happen faster than the animation duration.
  // eslint-disable-next-line no-unused-expressions
  el.offsetWidth;
  el.classList.remove("flashing");
  el.classList.add("flashing");
  // Schedule fade back to idle. setTimeout ID is held so a faster subsequent
  // refresh can cancel and reschedule cleanly.
  if (state.refreshIndicatorTimer) clearTimeout(state.refreshIndicatorTimer);
  state.refreshIndicatorTimer = setTimeout(() => {
    el.classList.remove("flashing");
    state.refreshIndicatorTimer = null;
  }, 750);
}

// Format a fractional rate (0..1, or 0.0 from the backend when dispatched
// count is zero) as a percentage string. We never want to render "NaN%" or
// "undefined%" — a denominator of 0 must still produce "0%".
function fmtPct(rate) {
  const v = Number(rate);
  if (!Number.isFinite(v)) return "0%";
  return `${Math.round(v * 100)}%`;
}

// Render the fleet Overview landing view into #plan-detail. This is the
// default view whenever no plan is selected and is also what selectOverview
// navigates back to from a selected plan.
//
// `plansPayload` is the body of /api/plans ({plans: [...]}) and
// `healthPayload` is the body of /api/dispatch_health ({totals, per_plan}).
// Both are passed in (rather than re-fetched) so renderOverview can be
// unit-tested by callers that already have the JSON in hand.
//
// Empty/zero payloads must render cleanly — no NaN, no throws — so the
// dashboard looks sensible before any plan has shipped a story.
function renderOverview(plansPayload, healthPayload) {
  const section = document.getElementById("plan-detail");
  if (!section) return;

  const plans = (plansPayload && Array.isArray(plansPayload.plans))
    ? plansPayload.plans
    : [];
  const health = healthPayload || {};
  const withAcc = health.with_acceptance
    || { dispatched: 0, done: 0, escalated: 0, stories: 0,
         escalation_rate: 0.0, success_rate: 0.0 };
  const withoutAcc = health.without_acceptance
    || { dispatched: 0, done: 0, escalated: 0, stories: 0,
         escalation_rate: 0.0, success_rate: 0.0 };

  // Fleet totals. Every count is total-stories-derived (the story_count
  // field), so a plan with no stories simply contributes 0 to totals and
  // an empty status_counts object — the breakdown bar handles missing
  // keys by treating them as zero.
  let totalPlans = plans.length;
  let totalStories = 0;
  let totalDone = 0;
  const statusTotals = Object.create(null);
  for (const plan of plans) {
    const sc = plan.status_counts || {};
    totalStories += plan.story_count || 0;
    totalDone += sc.done || 0;
    for (const [k, v] of Object.entries(sc)) {
      statusTotals[k] = (statusTotals[k] || 0) + (v || 0);
    }
  }

  // Per-status breakdown bar — a single horizontal bar split into the
  // STATUS_COLUMNS, each segment proportional to its share of totalStories.
  // Zero totalStories -> an empty-state placeholder rather than a 0-width
  // bar with bogus percentages.
  let breakdownBarHtml;
  if (totalStories === 0) {
    breakdownBarHtml =
      `<div class="overview-empty">No stories yet.</div>`;
  } else {
    const segments = STATUS_COLUMNS
      .map((s) => {
        const n = statusTotals[s] || 0;
        if (!n) return "";
        const pct = (n / totalStories) * 100;
        return `<div class="overview-bar-seg" data-status="${s}"
            style="width:${pct.toFixed(2)}%;background:var(--c-${s})"
            title="${s}: ${n}"></div>`;
      })
      .join("");
    const legend = STATUS_COLUMNS
      .filter((s) => statusTotals[s])
      .map((s) => `<span class="overview-legend-item">
          <span class="overview-legend-dot" style="background:var(--c-${s})"></span>
          ${escapeHtml(s)} ${statusTotals[s]}
        </span>`)
      .join("");
    breakdownBarHtml = `
      <div class="overview-bar">${segments}</div>
      <div class="overview-legend">${legend}</div>
    `;
  }

  section.innerHTML = `
    <div class="overview">
      <h2 class="overview-title">Fleet Overview</h2>

      <section class="overview-section overview-totals">
        <div class="overview-stat">
          <div class="overview-stat-value">${totalPlans}</div>
          <div class="overview-stat-label">plans</div>
        </div>
        <div class="overview-stat">
          <div class="overview-stat-value">${totalStories}</div>
          <div class="overview-stat-label">stories</div>
        </div>
        <div class="overview-stat">
          <div class="overview-stat-value">${totalDone}</div>
          <div class="overview-stat-label">done</div>
        </div>
      </section>

      <section class="overview-section">
        <h3 class="overview-section-title">Status breakdown</h3>
        ${breakdownBarHtml}
      </section>

      <section class="overview-section overview-health">
        <h3 class="overview-section-title">Dispatch health by acceptance</h3>
        <p class="overview-section-help">
          Stories are split by whether their plan carried an
          <code>acceptance</code> block at dispatch time. Watch the
          <em>with_acceptance</em> rate trend down relative to
          <em>without_acceptance</em> &mdash; that's the Fix #1 oracle paying off.
        </p>
        <div class="overview-stat-cards">
          <div class="overview-stat-card">
            <div class="overview-stat-card-title">with acceptance</div>
            <div class="overview-stat-card-metric">
              <span class="overview-stat-card-num">${fmtPct(withAcc.escalation_rate)}</span>
              <span class="overview-stat-card-kind">escalation</span>
            </div>
            <div class="overview-stat-card-metric">
              <span class="overview-stat-card-num">${fmtPct(withAcc.success_rate)}</span>
              <span class="overview-stat-card-kind">success</span>
            </div>
            <div class="overview-stat-card-foot">
              ${withAcc.dispatched || 0} dispatched &middot;
              ${withAcc.escalated || 0} escalated &middot;
              ${withAcc.done || 0} done
            </div>
          </div>
          <div class="overview-stat-card">
            <div class="overview-stat-card-title">without acceptance</div>
            <div class="overview-stat-card-metric">
              <span class="overview-stat-card-num">${fmtPct(withoutAcc.escalation_rate)}</span>
              <span class="overview-stat-card-kind">escalation</span>
            </div>
            <div class="overview-stat-card-metric">
              <span class="overview-stat-card-num">${fmtPct(withoutAcc.success_rate)}</span>
              <span class="overview-stat-card-kind">success</span>
            </div>
            <div class="overview-stat-card-foot">
              ${withoutAcc.dispatched || 0} dispatched &middot;
              ${withoutAcc.escalated || 0} escalated &middot;
              ${withoutAcc.done || 0} done
            </div>
          </div>
        </div>
      </section>

      <section class="overview-section">
        <h3 class="overview-section-title">Plans</h3>
        <ul class="overview-plan-list" id="overview-plan-list"></ul>
      </section>
    </div>
  `;

  // Looked up via getElementById (not section.querySelector): section here
  // comes from document.getElementById, and some DOM shims (see
  // tests/unit/test_dashboard_comms_nav.py, which drives this function via
  // selectComms()/selectOverview()) only implement querySelectorAll, not
  // querySelector.
  _diffOverviewPlanRows(document.getElementById("overview-plan-list"), plans);
}

// Shared builder for the overview compact plan-row `.overview-plan-meta`
// line, mirroring _planMetaMarkup so both branches of _diffOverviewPlanRows
// (new row / in-place update) produce byte-identical markup.
function _overviewPlanMetaMarkup(plan) {
  const total = plan.story_count || 0;
  const done = (plan.status_counts && plan.status_counts.done) || 0;
  const pausedTag = plan.paused
    ? ` <span class="plan-paused">paused</span>`
    : "";
  return `${done}/${total}${pausedTag}`;
}

// Build a single `.overview-plan-row` <li>. Used by _diffOverviewPlanRows
// to create exactly one new row for a plan that isn't in
// overviewPlanRowsByName yet.
function _buildOverviewPlanRow(plan) {
  const li = document.createElement("li");
  li.className = "overview-plan-row";
  li.setAttribute("data-plan", plan.name);
  li.innerHTML = `
    <span class="overview-plan-name">${escapeHtml(plan.name)}</span>
    <span class="overview-plan-meta">${_overviewPlanMetaMarkup(plan)}</span>
  `;
  return li;
}

// Keyed-diff for renderOverview's compact plan-row list, mirroring
// renderPlanList's per-plan add/update/remove-by-name pattern (see
// planListRowsByName): existing rows are updated in place and moved into
// the freshly-built <ul> rather than recreated, keyed by plan name via
// each row's data-plan attribute. renderOverview rebuilds the rest of the
// section every poll tick, so overviewPlanRowsByName (not listEl's own
// prior children) is what lets a row's node identity survive across ticks.
function _diffOverviewPlanRows(listEl, plans) {
  if (!listEl) return;

  if (!plans.length) {
    for (const [, el] of overviewPlanRowsByName) el.remove();
    overviewPlanRowsByName.clear();
    const empty = document.createElement("li");
    empty.className = "overview-empty";
    empty.textContent = "No plans yet.";
    listEl.appendChild(empty);
    return;
  }

  const seen = new Set();
  for (const plan of plans) {
    seen.add(plan.name);
    const existing = overviewPlanRowsByName.get(plan.name);
    if (existing) {
      // Reuse the existing node: update its meta text in place, then move
      // it (appendChild on an already-attached node relocates it) into the
      // current listEl so row order still matches server-sorted `plans`.
      const meta = existing.querySelector(".overview-plan-meta");
      if (meta) {
        meta.textContent = _overviewPlanMetaMarkup(plan);
        meta.innerHTML = _overviewPlanMetaMarkup(plan);
      }
      listEl.appendChild(existing);
    } else {
      const row = _buildOverviewPlanRow(plan);
      listEl.appendChild(row);
      overviewPlanRowsByName.set(plan.name, row);
    }
  }

  // Drop rows for plans no longer present.
  for (const [name, el] of overviewPlanRowsByName) {
    if (!seen.has(name)) {
      el.remove();
      overviewPlanRowsByName.delete(name);
    }
  }
}

// Navigate back to the fleet Overview landing view. Clears the selected
// plan and re-renders the sidebar so the Overview item shows as active.
function selectOverview() {
  state.commsActive = false;
  state.configActive = false;
  state.selectedPlan = null;
  updateHash();
  const nav = document.getElementById("plan-list");
  if (nav) {
    for (const child of nav.children) {
      const isOverview = child.dataset && child.dataset.overview === "true";
      child.classList.toggle("active", isOverview);
    }
  }
  // Render with whatever data we already have in state so the click is
  // instant. The next refresh() will swap in fresh numbers.
  renderOverview(
    state.lastPlans || { plans: [] },
    state.lastHealth || null,
  );
}

function selectComms() {
  state.commsActive = true;
  state.configActive = false;
  state.selectedPlan = null;
  updateHash();
  const nav = document.getElementById("plan-list");
  if (nav) {
    for (const child of nav.children) {
      const isComms = child.dataset && child.dataset.comms === "true";
      child.classList.toggle("active", isComms);
    }
  }
  renderOverview(
    state.lastPlans || { plans: [] },
    state.lastHealth || null,
  );
}

async function selectPlan(name) {
  state.commsActive = false;
  state.configActive = false;
  state.selectedPlan = name;
  updateHash();
  await refresh();
}

function renderUsage(usage) {
  const banner = document.getElementById("usage-banner");
  if (!usage || !usage.available) {
    banner.classList.add("hidden");
    return;
  }
  banner.classList.remove("hidden");
  if (usage.gate_blind) {
    // Failing open: spend is unguarded. Make it impossible to miss.
    banner.classList.add("blind");
    const fails = usage.consecutive_parse_failures || 0;
    banner.innerHTML =
      `&#9888; <strong>Claude usage gate is BLIND</strong> — the usage probe has ` +
      `been unparseable (${fails} consecutive failures) since ` +
      `${escapeHtml(usage.blind_since || "?")}, so the gate is failing OPEN and ` +
      `spend is unguarded. Check the <code>claude -p /cost</code> output / poller.`;
  } else {
    banner.classList.remove("blind");
    const s = usage.session_pct, w = usage.week_pct;
    const paused = usage.paused ? " &middot; <strong>PAUSED</strong>" : "";
    banner.innerHTML =
      `Usage gate: session ${s}% &middot; week ${w}%${paused} ` +
      `<span class="muted">(measured ${escapeHtml(usage.measured_at || "?")})</span>`;
  }
}

const COMMS_VIEW_ID = "comms-view";

function _applyActiveView() {
  const commsEl = document.getElementById(COMMS_VIEW_ID);
  const planDetailEl = document.getElementById("plan-detail");
  const configEl = document.getElementById("config-view");
  if (!commsEl || !planDetailEl || !configEl) return;
  if (state.configActive) {
    configEl.classList.remove("hidden");
    commsEl.classList.add("hidden");
    planDetailEl.classList.add("hidden");
  } else if (state.commsActive) {
    configEl.classList.add("hidden");
    commsEl.classList.remove("hidden");
    planDetailEl.classList.add("hidden");
  } else {
    configEl.classList.add("hidden");
    commsEl.classList.add("hidden");
    planDetailEl.classList.remove("hidden");
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


async function refresh() {
  _applyActiveView();
  try {
    renderUsage(await fetchJson("/api/usage"));
  } catch {
    /* usage state unavailable; leave the banner as-is */
  }

  const plansUrl = state.showArchived ? "/api/plans?include_archived=true" : "/api/plans";
  const { plans } = await fetchJson(plansUrl);
  state.lastPlans = { plans };
  renderPlanList(plans);

  // Toast handling: pick new notifications and push toasts
  if (!hasSeededNotifications) {
    // Seed seen map without pushing toasts
    for (const plan of plans) {
      const rec = plan.latest_notification;
      if (rec && rec.dedup_key != null) {
        lastSeenNotificationByPlan.set(plan.name, rec.dedup_key);
      }
    }
    hasSeededNotifications = true;
  } else {
    const newNotifs = pickNewNotifications(plans, lastSeenNotificationByPlan);
    for (const { plan, record } of newNotifs) {
      pushToast({ severity: record.severity, planName: plan.name, storyKey: record.story_key, message: record.message });
      lastSeenNotificationByPlan.set(plan.name, record.dedup_key);
    }
  }





  // Fleet dispatch health is consumed by the Overview landing view. Fetch
  // it every refresh so the headline rates stay live alongside the plan
  // list. Failure here must not block the rest of the refresh — we
  // gracefully fall back to all-zeros on the Overview.
  let health = null;
  try {
    health = await fetchJson("/api/dispatch_health");
  } catch {
    health = null;
  }
  state.lastHealth = health;

  if (state.selectedPlan) {
    try {
      const plan = await fetchJson(`/api/plans/${encodeURIComponent(state.selectedPlan)}`);
      setDiffNotificationsOnPoll(true);
      try {
        renderPlanDetail(plan);
      } finally {
        setDiffNotificationsOnPoll(false);
      }
      // Poll-triggered update: append only new notification rows to the
      // already-rendered notifications panel body instead of rebuilding it.
      const notifPanelBody = document.querySelector('#plan-detail .panel .panel-body');
      if (notifPanelBody) _diffNotificationsPanel(notifPanelBody, plan.notification_records);
    } catch {
      state.selectedPlan = null;
    }
  } else {
    // No plan selected -> render the fleet Overview into #plan-detail.
    renderOverview({ plans }, health);
  }

  document.getElementById("last-updated").textContent =
    `updated ${new Date().toLocaleTimeString()}`;

  // Visible liveness: a brief indicator flash on each successful refresh,
  if (state.pollHandle) flashRefreshIndicator();
}

function startPolling() {
  if (state.pollHandle) clearInterval(state.pollHandle);
  state.pollHandle = setInterval(refresh, 4000);
}

function stopPolling() {
  if (state.pollHandle) clearInterval(state.pollHandle);
  state.pollHandle = null;
}

function syncPollingWithVisibility() {
  if (document.hidden) {
    stopPolling();
  } else if (!state.pollHandle) {
    startPolling();
    // Catch up on one immediate refresh so the user sees fresh data the
    // moment they return to the tab instead of waiting up to 4s for the
    // next tick.
    refresh();
  }
}

document.getElementById("story-modal-close").addEventListener("click", hideStoryModal);
document.getElementById("story-modal-body").addEventListener("click", handleCopyClick);
document.getElementById("story-modal").addEventListener("click", (e) => {
  // Click on the backdrop (outside the modal-content) closes the modal.
  if (e.target.id === "story-modal") hideStoryModal();
});

// Pause / resume the polling loop around tab visibility. visibilitychange
// fires on tab switch, minimize, and on some browsers when the window
// loses focus to the OS — exactly the moments we want to stop polling.
document.addEventListener("visibilitychange", syncPollingWithVisibility);

// === Theme toggle =========================================================
// Persists choice in localStorage under THEME_KEY. Defaults to "dark" when
// unset/empty. Wrapped in try/catch so a locked-down browser (or any
// document without Storage permission) doesn't break the page.
const THEME_KEY = "pipeline-dashboard-theme";
const DEFAULT_THEME = "dark";
const VALID_THEMES = new Set(["dark", "light"]);

function readStoredTheme() {
  try {
    const raw = localStorage.getItem(THEME_KEY);
    if (!raw) return DEFAULT_THEME;
    const value = String(raw).trim().toLowerCase();
    return VALID_THEMES.has(value) ? value : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

function writeStoredTheme(theme) {
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* localStorage unavailable; theme simply won't persist */
  }
}

function applyTheme(theme) {
  const t = VALID_THEMES.has(theme) ? theme : DEFAULT_THEME;
  document.documentElement.dataset.theme = t;
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    btn.setAttribute("aria-pressed", t === "light" ? "true" : "false");
    btn.title = t === "dark" ? "Switch to light theme" : "Switch to dark theme";
  }
}

function initTheme() {
  applyTheme(readStoredTheme());
}

function toggleTheme() {
  const current = document.documentElement.dataset.theme === "light" ? "light" : "dark";
  const next = current === "light" ? "dark" : "light";
  applyTheme(next);
  writeStoredTheme(next);
}

(function wireThemeToggle() {
  const btn = document.getElementById("theme-toggle");
  if (btn) btn.addEventListener("click", toggleTheme);
})();

initTheme();
resetState();
resetPlanListState();
resetPlanDetailState();
loadFilters();
// Apply the URL hash (if any) before the first refresh — hash wins over
// localStorage. Also wire the browser's hashchange event so back/forward
// re-selects plans / re-applies filters without a full reload.
applyHashToState();
window.addEventListener("hashchange", applyHashToState);
refresh();
startPolling();

// Expose helpers for node-based smoke tests. Guarded so the file still works
// as a plain browser <script>.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    capturePlanDetailState, restorePlanDetailState, flashRefreshIndicator,
    startPolling, stopPolling, syncPollingWithVisibility, renderPlanDetail,
    showStoryModal, _renderStoryModalBody, handleCopyClick,
    filterStoryNotifications, renderStoryModalNotifications,
    renderOverview, selectOverview, refresh, state,
    renderPlanList, _renderPlanListFull,
    _diffOverviewPlanRows,
    renderNotifications,
    _diffNotificationsPanel,
    renderChecklist,
    filterNotifications,
    pickNewNotifications,
    pushToast,
    renderBoard,
    selectComms,
    _applyActiveView,
    sendCommsMessage,
    appendCommsMessage,
    renderToolTraceHtml,
    _diffBoardCards,
    renderConfigView,
    _renderConfigRoles,
    _renderConfigEnv,
    _renderConfigIgnored,
    _saveRoleConfig,
    _wireBackendSelector,
  };
}

// ---------------------------------------------------------------------------
// Configuration view (W3b-B4b)
// ---------------------------------------------------------------------------
// Fetch GET /api/config (optionally layered with ?plan=<selectedPlan>) and
// render the Roles / Environment Variables / Ignored Env Vars tables. Wire
// each role's edit Save button to the B3 write endpoints and surface 400
// validation errors inline. The registry (model_registry.json) supplies the
// provider/model dropdown options; if it is unreachable we degrade to the
// providers already present in the effective config so the edit controls
// never render empty.

async function renderConfigView() {
  const section = document.getElementById("config-view");
  if (!section) return;
  state.configActive = true;
  state.commsActive = false;
  _applyActiveView();

  const plan = state.selectedPlan || null;
  const url = plan
    ? `/api/config?plan=${encodeURIComponent(plan)}`
    : "/api/config";
  let cfg;
  try {
    cfg = await fetchJson(url);
  } catch (e) {
    renderConfigError(section, `Failed to load configuration: ${e.message}`);
    return;
  }
  const registry = await loadRegistry();
  _renderConfigRoles(section, cfg.roles || [], registry, plan);
  _renderConfigEnv(section, cfg.env || []);
  _renderConfigIgnored(section, cfg.ignored_env_vars || []);
}

function renderConfigError(section, message) {
  const rolesBody = section.querySelector("#roles-table tbody");
  if (rolesBody) {
    rolesBody.innerHTML =
      `<tr><td colspan="8" class="config-error">${escapeHtml(message)}</td></tr>`;
  }
}

async function loadRegistry() {
  try {
    const res = await fetch("/model_registry.json");
    if (!res.ok) return {};
    return await res.json();
  } catch {
    return {};
  }
}

function _renderConfigRoles(section, roles, registry, plan) {
  const tbody = section.querySelector("#roles-table tbody");
  if (!tbody) return;
  if (!Array.isArray(roles) || roles.length === 0) {
    tbody.innerHTML =
      `<tr><td colspan="8" class="empty-state">No roles configured.</td></tr>`;
    return;
  }
  const providers = (registry && registry.providers) || {};
  tbody.innerHTML = roles.map((role) => {
    const immediate = role.provider_source === "plan_role_config";
    const restartBadge = role.restart_required
      ? `<span class="restart-required" title="A restart is required for this change to take effect">restart required</span>`
      : (immediate
          ? `<span class="config-immediate" title="Takes effect immediately">immediate</span>`
          : "");
    const errorCell = role.error
      ? `<span class="config-error" title="${escapeHtml(role.error)}">${escapeHtml(role.error)}</span>`
      : "";
    const edit = renderRoleEdit(role, providers);
    return `<tr data-role="${escapeHtml(role.role)}">
      <td>${escapeHtml(role.role)}</td>
      <td>${escapeHtml(role.provider || "—")}</td>
      <td>${escapeHtml(role.model || "—")}</td>
      <td>${escapeHtml(role.provider_source || "—")}</td>
      <td>${escapeHtml(role.model_source || "—")}</td>
      <td>${restartBadge}</td>
      <td>${edit}</td>
      <td>${errorCell}</td>
    </tr>`;
  }).join("");

  tbody.querySelectorAll("[data-save-role]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = btn.closest("tr");
      const role = row.dataset.role;
      const provider = row.querySelector(".edit-provider").value;
      const model = row.querySelector(".edit-model").value;
      const errorEl = row.querySelector(".edit-error");
      _saveRoleConfig(role, provider, model, plan, errorEl);
    });
  });
}

function renderRoleEdit(role, providers) {
  const providerNames = Object.keys(providers || {});
  const currentProvider = role.provider || "";
  if (providerNames.indexOf(currentProvider) === -1 && currentProvider) {
    providerNames.unshift(currentProvider);
  }
  const providerOpts = providerNames.map((p) =>
    `<option value="${escapeHtml(p)}" ${p === currentProvider ? "selected" : ""}>${escapeHtml(p)}</option>`
  ).join("");
  const models = (providers[currentProvider] && providers[currentProvider].models) || {};
  const modelNames = Object.keys(models);
  const currentModel = role.model || "";
  if (modelNames.indexOf(currentModel) === -1 && currentModel) {
    modelNames.unshift(currentModel);
  }
  const modelOpts = modelNames.map((m) =>
    `<option value="${escapeHtml(m)}" ${m === currentModel ? "selected" : ""}>${escapeHtml(m)}</option>`
  ).join("");
  return `
    <select class="edit-provider" aria-label="Provider for ${escapeHtml(role.role)}">${providerOpts}</select>
    <select class="edit-model" aria-label="Model for ${escapeHtml(role.role)}">${modelOpts}</select>
    <button type="button" class="edit-save" data-save-role>Save</button>
    <span class="edit-error config-error"></span>
  `;
}

async function _saveRoleConfig(role, provider, model, plan, errorEl) {
  const url = plan
    ? `/api/config/plans/${encodeURIComponent(plan)}/roles/${encodeURIComponent(role)}`
    : `/api/config/roles/${encodeURIComponent(role)}`;
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider, model }),
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const body = await res.json();
        if (body && body.detail) detail = body.detail;
      } catch { /* non-JSON error body */ }
      if (errorEl) errorEl.textContent = detail;
      return;
    }
    if (errorEl) errorEl.textContent = "";
    // Re-fetch so the effective config reflects the new value.
    renderConfigView();
  } catch (e) {
    if (errorEl) errorEl.textContent = e.message;
  }
}

function _renderConfigEnv(section, env) {
  const tbody = section.querySelector("#env-vars-table tbody");
  if (!tbody) return;
  if (!Array.isArray(env) || env.length === 0) {
    tbody.innerHTML =
      `<tr><td colspan="6" class="empty-state">No environment variables cataloged.</td></tr>`;
    return;
  }
  tbody.innerHTML = env.map((v) => {
    const restartBadge = v.restart_required
      ? `<span class="restart-required" title="A restart is required for this change to take effect">restart required</span>`
      : "";
    const conflict = v.conflict
      ? `<span class="config-conflict" title="Conflicting values across config layers">conflict</span>`
      : "";
    const layers = Array.isArray(v.layers) && v.layers.length
      ? v.layers.map((l) => `${escapeHtml(l.layer)}${l.restart_required ? " (restart)" : ""}`).join(" → ")
      : "";
    const value = v.masked
      ? "***"
      : (v.effective === undefined || v.effective === null
          ? "—"
          : escapeHtml(String(v.effective)));
    return `<tr>
      <td>${escapeHtml(v.name)}</td>
      <td class="mono">${value}</td>
      <td>${escapeHtml(v.source || "—")}</td>
      <td>${restartBadge}</td>
      <td>${conflict}</td>
      <td>${escapeHtml(layers)}</td>
    </tr>`;
  }).join("");
}

function _renderConfigIgnored(section, ignored) {
  const panel = section.querySelector("#ignored-vars-panel");
  if (!panel) return;
  if (!Array.isArray(ignored) || ignored.length === 0) {
    panel.innerHTML =
      `<p class="empty-state">No ignored environment variables present.</p>`;
    return;
  }
  panel.innerHTML = ignored.map((v) => `
    <div class="ignored-card">
      <strong>${escapeHtml(v.name)}</strong>
      <p>Use <code>${escapeHtml(v.use_instead || "")}</code> instead.</p>
      <p class="muted">${escapeHtml(v.reason || "")}</p>
    </div>
  `).join("");
}

// ---------------------------------------------------------------------------
// Per-story backend selector (W3b-B4b)
// ---------------------------------------------------------------------------
// The story modal carries a <select id="backend-select">. On change we PATCH
// the story via POST /api/plans/{plan}/stories/{story}/patch with
// {backend: <value>} and surface a 400 inline. The current backend is set on
// the select whenever the modal opens (see _renderStoryModalBody).

function _backendErrorEl() {
  let el = document.getElementById("backend-error");
  if (!el) {
    const selector = document.getElementById("backend-selector");
    if (!selector) return null;
    el = document.createElement("span");
    el.id = "backend-error";
    el.className = "config-error";
    selector.appendChild(el);
  }
  return el;
}

function _wireBackendSelector() {
  const select = document.getElementById("backend-select");
  if (!select) return;
  select.addEventListener("change", async () => {
    const modal = document.getElementById("story-modal");
    if (!modal) return;
    const plan = modal.dataset.plan;
    const key = modal.dataset.story;
    const errorEl = _backendErrorEl();
    if (!plan || !key) return;
    const backend = select.value;
    try {
      const res = await fetch(
        `/api/plans/${encodeURIComponent(plan)}/stories/${encodeURIComponent(key)}/patch`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ backend }),
        }
      );
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const body = await res.json();
          if (body && body.detail) detail = body.detail;
        } catch { /* non-JSON error body */ }
        if (errorEl) errorEl.textContent = detail;
        return;
      }
      if (errorEl) errorEl.textContent = "";
    } catch (e) {
      if (errorEl) errorEl.textContent = e.message;
    }
  });
}

// Wire the Configuration nav item (in #config-nav) to open the config view.
(function wireConfigNav() {
  const nav = document.getElementById("config-nav");
  if (!nav) return;
  const item = document.createElement("div");
  item.className = "plan-item config-item";
  item.innerHTML = `<div class="plan-name">Config</div><div class="plan-meta">roles &amp; env</div>`;
  item.addEventListener("click", () => renderConfigView());
  nav.appendChild(item);
})();

_wireBackendSelector();

export { state, defaultFilters, loadFilters, saveFilters, STATUS_COLUMNS, BACKEND_VALUES, ESCALATED_VALUES, FILTERS_KEY } from "./app/state.js";

export { hashStateFrom, encodeHashState, parseHash, updateHash, clearHash, applyHashToState } from "./app/routing.js";

export {
  STALE_IN_PROGRESS_MINUTES, _diffBoardCards, ageLabelFor, applyFilters, chip,
  escapeHtml, isStaleInProgress, relativeAgeLabel, renderBoard, renderFilterBar,
} from "./app/render/board.js";

export {
  NOTIF_SEVERITY_COLOR,
  _applyActiveView,
  _diffNotificationsPanel, _diffOverviewPlanRows,
  appendCommsMessage,
  filterNotifications,
  flashRefreshIndicator,
  notifSeverityFilter, setNotifSeverityFilter,
  pickNewNotifications, pushToast, refresh,
  renderDecisions, renderNotifications, renderOverview,
  renderToolTraceHtml,
  selectComms, selectOverview, selectPlan,
  sendCommsMessage, startPolling, stopPolling,
  syncPollingWithVisibility,
};

export {
  renderPlanList, _renderPlanListFull, _buildPlanRow, togglePlanArchived,
  planListRowsByName,
} from "./app/render/plan-list.js";

export {
  renderPlanDetail, capturePlanDetailState, restorePlanDetailState,
  renderChecklist, renderJournal, renderJournalEntry, handleCopyClick,
} from "./app/render/plan-detail.js";

export {
  filterStoryNotifications, renderStoryModalNotifications,
  showStoryModal, _renderStoryModalBody,
} from "./app/render/story-modal.js";
