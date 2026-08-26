import {
  state, loadFilters, resetState,
  SORT_OPTIONS, VALID_SORTS, BACKEND_VALUES, ESCALATED_VALUES,
} from "./state.js";
import { fetchJson } from "./api.js";
import {
  HASH_KEYS, hashStateFrom, encodeHashState, parseHash, updateHash, clearHash, applyHashToState,
} from "./routing.js";
import {
  escapeHtml, renderBoard,
} from "./render/board.js";
import {
  renderPlanList, _renderPlanListFull, resetPlanListState, initPlanList,
} from "./render/plan-list.js";
import {
  renderPlanDetail, capturePlanDetailState, restorePlanDetailState,
  setDiffNotificationsOnPoll, resetPlanDetailState, initPlanDetail, renderChecklist,
  loadStoryJournal, loadStoryChecklist, renderCopyButton, handleCopyClick, copyValues,
} from "./render/plan-detail.js";
import {
  initStoryModal, filterStoryNotifications,
  renderStoryModalNotifications, showStoryModal, _renderStoryModalBody,
  hideStoryModal,
} from "./render/story-modal.js";
import {
  initNotifications, NOTIF_SEVERITY_COLOR, pickNewNotifications, pushToast,
  setNotifSeverityFilter, renderNotifications, _diffNotificationsPanel,
  filterNotifications, notificationKey,
} from "./render/notifications.js";
import { renderDecisions } from "./render/decisions.js";
import { renderOverview, _diffOverviewPlanRows } from "./render/overview.js";
import { renderToolTraceHtml, appendCommsMessage, sendCommsMessage, updateCommsSubtitle } from "./comms.js";
import { renderUsage } from "./usage.js";

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

// plan-list.js/plan-detail.js/story-modal.js/notifications.js can't
// statically import back from app.js (see their own comments on this)
// without breaking under the test harness's cache-busted app.js URL, so this
// wires their few app.js-level dependencies via injection instead. Safe to
// call immediately: selectPlan, selectComms, selectOverview, refresh, and
// _backendErrorEl are hoisted local function declarations, available before
// this line runs regardless of source order; renderNotifications,
// renderDecisions, and setNotifSeverityFilter are imported bindings, which
// (like hoisted declarations) are resolved before this module's top-level
// code runs.
initPlanList({ selectPlan, selectComms, selectOverview, refresh });
initPlanDetail({ showStoryModal, renderNotifications, renderDecisions, setNotifSeverityFilter });
initStoryModal({ backendErrorEl: _backendErrorEl, notifSeverityColor: NOTIF_SEVERITY_COLOR });
initNotifications({ selectComms });

// Toast state
let lastSeenNotificationByPlan = new Map();
let hasSeededNotifications = false;

// Flash the header refresh indicator. Called once per successful refresh
// so the user sees liveness without staring at the clock. The .flashing class
// drives the dot's pulse animation. We toggle it off after the animation so the element returns to its idle (invisible) state.
function flashRefreshIndicator() {
  const el = document.getElementById("refresh-indicator");
  if (!el) return;
  // Re-render contents each time so the animation restarts cleanly even on
  // back-to-back flashes that would otherwise be coalesced by the browser.
  el.innerHTML = '<span class="dot" aria-hidden="true"></span>'
    + '<span class="refresh-indicator-label">updating…</span>';
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
  updateCommsSubtitle();
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
      if (rec) {
        lastSeenNotificationByPlan.set(plan.name, notificationKey(rec));
      }
    }
    hasSeededNotifications = true;
  } else {
    const newNotifs = pickNewNotifications(plans, lastSeenNotificationByPlan);
    for (const { plan, record } of newNotifs) {
      pushToast({ severity: record.severity, planName: plan.name, storyKey: record.story_key, message: record.message });
      lastSeenNotificationByPlan.set(plan.name, notificationKey(record));
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
  }
}

export { state, defaultFilters, loadFilters, saveFilters, STATUS_COLUMNS, BACKEND_VALUES, ESCALATED_VALUES, FILTERS_KEY } from "./state.js";

export { hashStateFrom, encodeHashState, parseHash, updateHash, clearHash, applyHashToState } from "./routing.js";

export {
  STALE_IN_PROGRESS_MINUTES, _diffBoardCards, ageLabelFor, applyFilters, chip,
  escapeHtml, isStaleInProgress, relativeAgeLabel, renderBoard, renderFilterBar,
} from "./render/board.js";

export {
  _applyActiveView,
  appendCommsMessage,
  flashRefreshIndicator,
  refresh,
  renderToolTraceHtml,
  selectComms, selectOverview, selectPlan,
  sendCommsMessage, startPolling, stopPolling,
  syncPollingWithVisibility,
};

export {
  NOTIF_SEVERITY_COLOR, _diffNotificationsPanel, filterNotifications,
  notifSeverityFilter, setNotifSeverityFilter, pickNewNotifications,
  pushToast, renderNotifications,
} from "./render/notifications.js";

export { renderDecisions } from "./render/decisions.js";

export {
  _diffOverviewPlanRows, renderOverview,
} from "./render/overview.js";

export {
  renderPlanList, _renderPlanListFull, _buildPlanRow, togglePlanArchived,
  planListRowsByName,
} from "./render/plan-list.js";

export {
  renderPlanDetail, capturePlanDetailState, restorePlanDetailState,
  renderChecklist, renderJournal, renderJournalEntry, handleCopyClick,
} from "./render/plan-detail.js";

export {
  filterStoryNotifications, renderStoryModalNotifications,
  showStoryModal, _renderStoryModalBody,
} from "./render/story-modal.js";

export { renderUsage } from "./usage.js";
