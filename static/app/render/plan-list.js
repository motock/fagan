import { state } from "../state.js";
import { postJson } from "../api.js";
import { escapeHtml } from "./board.js";

// plan-list.js cannot statically `import ... from "../../app.js"`: app.js's
// dynamic-import test harness cache-busts its own URL with a `?t=` query
// param, so a static back-import here would resolve to a SECOND, separate
// app.js module instance and re-run its top-level init (polling, DOM wiring)
// out of order — reproduced as a real TDZ crash on `resetPlanListState()`.
// app.js instead calls initPlanList() once at init with its own function
// references, breaking the import-graph cycle while keeping identical
// call-time behavior.
let _selectPlan, _selectComms, _selectOverview, _refresh;
function initPlanList({ selectPlan, selectComms, selectOverview, refresh }) {
  _selectPlan = selectPlan;
  _selectComms = selectComms;
  _selectOverview = selectOverview;
  _refresh = refresh;
}

// Keyed-diff state for renderPlanList: maps each plan name to its live
// `.plan-item` DOM node so per-plan rows are updated in place across poll
// ticks instead of being torn down and rebuilt every 4s. `null` means the
// sidebar has never been rendered (first call does a full rebuild).
let planListRowsByName = null;

// Node's dynamic import() caches this module by URL, so its module-level
// state (planListRowsByName) is created once per process even when app.js
// itself is cache-busted and re-imported per test — call this from app.js's
// init sequence so each load starts from a known-default state, mirroring
// state.js's resetState().
function resetPlanListState() {
  planListRowsByName = null;
}

// Plans come back from /api/plans already sorted newest-first by the
// server (by manifest mtime) and, by default, with archived plans excluded
// - state.showArchived controls whether refresh() asks for them too (see
// the include_archived query param built there).
// Shared builder for the `.plan-meta` line, used by all three render paths
// (_renderPlanListFull, _buildPlanRow, and the keyed-diff in-place update) so
// they produce byte-identical markup — including the styled
// `<span class="plan-paused">paused</span>` badge for paused plans.
function _planMetaMarkup(plan) {
  const total = plan.story_count || 0;
  const done = (plan.status_counts && plan.status_counts.done) || 0;
  return `${done}/${total} done${plan.paused ? ' <span class="plan-paused">paused</span>' : ""}`;
}

function _renderPlanListFull(plans) {
  const nav = document.getElementById("plan-list");

  // This function no longer clears nav.innerHTML. renderPlanList's
  // first-call branch clears the nav itself and appends the pinned
  // Comms/Overview items and the "PLANS" section label BEFORE calling
  // this function, so the per-plan rows and the footer land underneath
  // them. Clearing here would wipe those pinned items.
  for (const plan of plans) {
    const div = document.createElement("div");
    div.className = "plan-item" + (plan.name === state.selectedPlan ? " active" : "")
      + (plan.archived ? " plan-archived" : "");
    div.setAttribute("data-plan-name", plan.name);
    div.innerHTML = `
      <div class="plan-item-row">
        <div class="plan-item-main">
          <div class="plan-name">${escapeHtml(plan.name)}</div>
          <div class="plan-meta">${_planMetaMarkup(plan)}</div>
        </div>
        <button type="button" class="plan-archive-btn" title="${plan.archived ? "Restore" : "Dismiss"}">
          ${plan.archived ? "Restore" : "Dismiss"}
        </button>
      </div>
    `;
    div.addEventListener("click", () => _selectPlan(plan.name));
    div.querySelector(".plan-archive-btn").addEventListener("click", (event) => {
      // Don't let the archive/restore click also select the plan.
      event.stopPropagation();
      togglePlanArchived(plan.name, plan.archived);
    });
    nav.appendChild(div);
  }

  // Built with direct DOM calls, not an innerHTML string: a bare
  // <input type="checkbox"> has no closing tag, and this file's lightweight
  // test-only DOM stubs parse innerHTML by matching open/close tag pairs -
  // a self-closing input embedded in an innerHTML string would silently
  // fail to parse into a real, listenable element under test.
  const toggle = document.createElement("div");
  toggle.className = "plan-list-footer";
  const label = document.createElement("label");
  label.className = "show-archived-toggle";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = state.showArchived;
  checkbox.addEventListener("change", (event) => {
    state.showArchived = event.target.checked;
    _refresh();
  });
  label.appendChild(checkbox);
  const labelText = document.createElement("span");
  labelText.textContent = "Show dismissed plans";
  label.appendChild(labelText);
  toggle.appendChild(label);
  nav.appendChild(toggle);
}

// Build a single per-plan `.plan-item` row (the same markup the full-rebuild
// loop in _renderPlanListFull produces) and return it. Used by the keyed-diff
// wrapper to create exactly one new row for a plan that isn't in the map yet.
function _buildPlanRow(plan) {
  const div = document.createElement("div");
  div.className = "plan-item" + (plan.name === state.selectedPlan ? " active" : "")
    + (plan.archived ? " plan-archived" : "");
  div.setAttribute("data-plan-name", plan.name);
  div.innerHTML = `
    <div class="plan-item-row">
      <div class="plan-item-main">
        <div class="plan-name">${escapeHtml(plan.name)}</div>
        <div class="plan-meta">${_planMetaMarkup(plan)}</div>
      </div>
      <button type="button" class="plan-archive-btn" title="${plan.archived ? "Restore" : "Dismiss"}">
        ${plan.archived ? "Restore" : "Dismiss"}
      </button>
    </div>
  `;
  div.addEventListener("click", () => _selectPlan(plan.name));
  div.querySelector(".plan-archive-btn").addEventListener("click", (event) => {
    // Don't let the archive/restore click also select the plan.
    event.stopPropagation();
    togglePlanArchived(plan.name, plan.archived);
  });
  return div;
}

// Insert a freshly-built plan row into the sidebar. New rows go just above
// the pinned footer toggle (which _renderPlanListFull always appends last),
// so plan rows stay grouped together; fall back to appending if the footer
// isn't present (e.g. a test shim without insertBefore).
function _insertPlanRow(nav, div) {
  const footer = nav.querySelector(".plan-list-footer");
  if (footer && typeof nav.insertBefore === "function") {
    nav.insertBefore(div, footer);
  } else {
    nav.appendChild(div);
  }
}

// Keyed-diff wrapper around _renderPlanListFull. The first call ever does a
// full rebuild and records each plan row in planListRowsByName; every later
// call diffs `plans` against that map so unchanged rows are updated in place
// (preserving in-progress interaction like an open right-click menu or a
// focused Dismiss button) instead of being torn down and rebuilt each poll
// tick. The pinned Comms/Overview items aren't part of the per-plan diff,
// but their .active class still depends on state that can change between
// polls, so it's refreshed here on every call (not just the first).
function renderPlanList(plans) {
  const nav = document.getElementById("plan-list");

  // The pinned Comms/Overview items are built once by _renderPlanListFull,
  // but their .active class must track state on every call (not just a
  // full rebuild), since refresh() calls renderPlanList on every poll tick.
  const commsItem = nav.querySelector && nav.querySelector("[data-comms]");
  if (commsItem) commsItem.classList.toggle("active", state.commsActive);
  const overviewItem = nav.querySelector && nav.querySelector("[data-overview]");
  if (overviewItem) overviewItem.classList.toggle("active", !state.selectedPlan && !state.commsActive);

  if (planListRowsByName === null) {
    nav.innerHTML = "";

    // Pinned Comms item - FIRST in the sidebar. The design treats it as the
    // sidebar's header pill (accent border + headset glyph), styled via
    // .comms-item / .icon-comms in style.css; it deliberately has no
    // .plan-meta subtitle, unlike the per-plan rows and Overview.
    const comms = document.createElement("div");
    comms.className = "plan-item comms-item" + (state.commsActive ? " active" : "");
    comms.setAttribute("data-comms", "true");
    comms.innerHTML = `
      <div class="plan-name"><span class="icon-comms" aria-hidden="true">&#127911;</span>Comms</div>
    `;
    comms.addEventListener("click", () => _selectComms());
    nav.appendChild(comms);

    // Pinned Overview item - SECOND, plain .plan-item styling, unchanged.
    const overview = document.createElement("div");
    overview.className = "plan-item overview-item" + (!state.selectedPlan ? " active" : "");
    overview.setAttribute("data-overview", "true");
    overview.innerHTML = `
      <div class="plan-name">Overview</div>
      <div class="plan-meta">fleet landing</div>
    `;
    overview.addEventListener("click", () => _selectOverview());
    nav.appendChild(overview);

    // "PLANS" section label, between the pinned global nav and the rows.
    const plansLabel = document.createElement("div");
    plansLabel.className = "plan-list-section-label";
    plansLabel.textContent = "PLANS";
    nav.appendChild(plansLabel);

    // Per-plan rows + the "Show dismissed plans" footer land AFTER the
    // pinned items because _renderPlanListFull no longer clears the nav.
    _renderPlanListFull(plans);
    planListRowsByName = new Map();
    // Query all .plan-item rows and keep only the per-plan ones (the pinned
    // Comms/Overview items also carry .plan-item but have no data-plan-name).
    for (const el of nav.querySelectorAll(".plan-item")) {
      if (el.getAttribute("data-plan-name")) {
        planListRowsByName.set(el.getAttribute("data-plan-name"), el);
      }
    }
    return;
  }

  const seen = new Set();
  for (let i = 0; i < plans.length; i++) {
    const plan = plans[i];
    seen.add(plan.name);
    const existing = planListRowsByName.get(plan.name);
    if (existing) {
      // Plan already rendered: update only the mutable fields on the EXISTING
      // node. Never recreate it — its event listeners are already bound to
      // the right plan.name closure from creation time.
      const meta = existing.querySelector(".plan-meta");
      if (meta) {
        // Set textContent first (so text-only DOM stubs that read
        // textContent see the updated meta), then innerHTML to the same
        // markup the builders emit — preserving the styled
        // <span class="plan-paused">paused</span> badge for paused plans.
        meta.textContent = _planMetaMarkup(plan);
        meta.innerHTML = _planMetaMarkup(plan);
      }
      existing.classList.toggle("active", plan.name === state.selectedPlan);
      existing.classList.toggle("plan-archived", !!plan.archived);
    } else {
      // New plan: build exactly one row and insert it at its server-sorted
      // (newest-first) position — before the row of the next plan in `plans`
      // that already exists, so the sidebar keeps the same order the
      // full-rebuild path renders. If it's the last new plan, append at the
      // end (just above the pinned footer).
      const div = _buildPlanRow(plan);
      let ref = null;
      for (let j = i + 1; j < plans.length; j++) {
        const nextRow = planListRowsByName.get(plans[j].name);
        if (nextRow) { ref = nextRow; break; }
      }
      if (ref) {
        nav.insertBefore(div, ref);
      } else {
        _insertPlanRow(nav, div);
      }
      planListRowsByName.set(plan.name, div);
    }
  }

  // Drop rows for plans no longer present.
  for (const [name, el] of planListRowsByName) {
    if (!seen.has(name)) {
      el.remove();
      planListRowsByName.delete(name);
    }
  }
}

// Archive/unarchive is fire-and-forget from the UI's perspective: on
// success, refresh() re-fetches /api/plans so the sidebar reflects the new
// archived set (and re-sorts/re-filters) rather than us hand-patching the
// DOM in place. A failed request leaves the list as-is; the user can retry.
async function togglePlanArchived(planName, currentlyArchived) {
  const action = currentlyArchived ? "unarchive" : "archive";
  try {
    await postJson(`/api/plans/${encodeURIComponent(planName)}/${action}`);
  } catch {
    return;
  }
  await _refresh();
}

export {
  renderPlanList, _renderPlanListFull, _buildPlanRow, togglePlanArchived,
  planListRowsByName, resetPlanListState, initPlanList,
};
