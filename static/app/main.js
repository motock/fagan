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
import { renderMaturityPanel } from "./render/maturity.js";
import { renderDecisions } from "./render/decisions.js";
import { renderOverview, _diffOverviewPlanRows } from "./render/overview.js";
import { renderToolTraceHtml, appendCommsMessage, sendCommsMessage, updateCommsSubtitle, resetCommsThread } from "./comms.js";
import { renderUsage } from "./usage.js";
import { fetchWorkspaces, selectWorkspace, fetchActiveWorkspace, renderWorkspacePicker } from "./workspace.js";

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
  state.workspaceActive = false;
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
  _applyActiveView();
}

function selectComms() {
  state.commsActive = true;
  state.configActive = false;
  state.workspaceActive = false;
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
  _applyActiveView();
}

async function selectPlan(name) {
  state.commsActive = false;
  state.configActive = false;
  state.workspaceActive = false;
  state.selectedPlan = name;
  updateHash();
  await refresh();
}

const COMMS_VIEW_ID = "comms-view";

function _applyActiveView() {
  const commsEl = document.getElementById(COMMS_VIEW_ID);
  const planDetailEl = document.getElementById("plan-detail");
  const configEl = document.getElementById("config-view");
  const workspaceEl = document.getElementById("workspace-view");
  if (!commsEl || !planDetailEl || !configEl) return;
  if (state.configActive) {
    configEl.classList.remove("hidden");
    commsEl.classList.add("hidden");
    planDetailEl.classList.add("hidden");
    if (workspaceEl) workspaceEl.classList.add("hidden");
  } else if (state.workspaceActive) {
    configEl.classList.add("hidden");
    commsEl.classList.add("hidden");
    planDetailEl.classList.add("hidden");
    if (workspaceEl) workspaceEl.classList.remove("hidden");
  } else if (state.commsActive) {
    configEl.classList.add("hidden");
    commsEl.classList.remove("hidden");
    planDetailEl.classList.add("hidden");
    if (workspaceEl) workspaceEl.classList.add("hidden");
  } else {
    configEl.classList.add("hidden");
    commsEl.classList.add("hidden");
    planDetailEl.classList.remove("hidden");
    if (workspaceEl) workspaceEl.classList.add("hidden");
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
    loadRegistry,
  };
}

export { loadRegistry };


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
    const res = await fetch("/api/config/providers");
    if (!res.ok) return {};
    const data = await res.json();
    return { providers: data.providers || {} };
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
wireWorkspaceView();

// ---------------------------------------------------------------------------
// Workspace view (workspace picker wiring)
// ---------------------------------------------------------------------------
// Fetch the workspace list + active workspace and render the picker into
// #workspace-picker. Never throws — fetchWorkspaces/fetchActiveWorkspace
// already resolve to [] / null on any failure, and renderWorkspacePicker([])
// already produces the picker's empty state.
async function loadWorkspaceView() {
  const picker = document.getElementById("workspace-picker");
  const workspaces = await fetchWorkspaces();
  const active = await fetchActiveWorkspace();
  if (picker) picker.innerHTML = renderWorkspacePicker(workspaces, active);
}

// Wire the Workspace nav item, the workspace form, and clicks on picker
// entries. Mirrors wireConfigNav's nav-click-opens-view shape while also
// handling workspace selection (form submit and picker-item click share the
// same result-handling closures below).
function wireWorkspaceView() {
  const nav = document.getElementById("workspace-nav");
  if (nav) {
    nav.addEventListener("click", async () => {
      state.workspaceActive = true;
      state.configActive = false;
      state.commsActive = false;
      _applyActiveView();
      await loadWorkspaceView();
    });
  }

  // Lazily created, memoized so repeated failures reuse the same element
  // instead of appending a new one to the form each time.
  let errorEl = null;
  const getErrorEl = () => {
    if (errorEl) return errorEl;
    const form = document.getElementById("workspace-form");
    if (!form) return null;
    errorEl = document.createElement("span");
    errorEl.id = "workspace-error";
    errorEl.className = "config-error";
    form.appendChild(errorEl);
    return errorEl;
  };

  const applyResult = async (result) => {
    if (result && result.ok) {
      state.selectedWorkspace = result.path;
      const el = getErrorEl();
      if (el) el.innerHTML = "";
      await loadWorkspaceView();
    } else {
      const el = getErrorEl();
      if (el) el.innerHTML = escapeHtml((result && result.error) || "unknown error");
    }
  };

  const form = document.getElementById("workspace-form");
  if (form) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = document.getElementById("workspace-path-input");
      const createBox = document.getElementById("workspace-create");
      const result = await selectWorkspace(input ? input.value : "", !!(createBox && createBox.checked));
      await applyResult(result);
    });
  }

  const picker = document.getElementById("workspace-picker");
  if (picker) {
    picker.addEventListener("click", async (e) => {
      const target = e && e.target;
      const item = target && target.closest ? target.closest("[data-path]") : target;
      const path = item && item.dataset ? item.dataset.path : undefined;
      if (!path) return;
      const result = await selectWorkspace(path, false);
      await applyResult(result);
    });
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
  resetCommsThread,
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
