import { state, defaultFilters, saveFilters, toggleFilter } from "../state.js";
import { updateHash } from "../routing.js";
import { fetchJson } from "../api.js";
import { renderBoard, renderFilterBar, escapeHtml } from "./board.js";
import { renderMaturityPanel } from "./maturity.js";

// plan-detail.js cannot statically `import ... from "../../app.js"`: app.js's
// dynamic-import test harness cache-busts its own URL with a `?t=` query
// param, so a static back-import here would resolve to a SECOND, separate
// app.js module instance and re-run its top-level init out of order (a real
// TDZ crash was reproduced from plan-list.js hitting this exact issue).
// app.js instead calls initPlanDetail() once at init with its own function
// references, breaking the import-graph cycle while keeping identical
// call-time behavior.
let _showStoryModal, _renderNotifications, _renderDecisions, _setNotifSeverityFilter;
function initPlanDetail({ showStoryModal, renderNotifications, renderDecisions, setNotifSeverityFilter }) {
  _showStoryModal = showStoryModal;
  _renderNotifications = renderNotifications;
  _renderDecisions = renderDecisions;
  _setNotifSeverityFilter = setNotifSeverityFilter;
}

// When true, renderPlanDetail's same-plan update path keeps the notifications
// panel body intact so _diffNotificationsPanel (called from refresh()) can
// append only new rows instead of rebuilding the whole panel. Set only around
// the poll-triggered render; filter-click re-renders leave it false.
let diffNotificationsOnPoll = false;

// app.js's refresh() toggles the poll-render mode around a renderPlanDetail
// call; ES module bindings can't be reassigned from outside the module that
// declares them, so this setter is the wiring app.js uses instead of direct
// assignment.
function setDiffNotificationsOnPoll(value) {
  diffNotificationsOnPoll = value;
}

// The plan object passed to the most recent renderPlanDetail call. The
// board's card-click listener is delegated (attached once to `.board`,
// not per-card — see renderPlanDetail) so it can't close over the `plan`
// argument from the render call that created it; it reads this instead,
// so a click always resolves against the freshest poll's data even though
// the listener itself was bound on an earlier render.
let currentPlanDetailData = null;

// Node's dynamic import() caches this module by URL, so its module-level
// state is created once per process even when app.js itself is cache-busted
// and re-imported per test — call this from app.js's init sequence so each
// load starts from a known-default state, mirroring state.js's resetState().
function resetPlanDetailState() {
  diffNotificationsOnPoll = false;
  currentPlanDetailData = null;
  copyValues.clear();
  openStoryRef.plan = null;
  openStoryRef.key = null;
}

// The header + filter bar — everything ABOVE the card board. Rebuilt
// wholesale on every renderPlanDetail call (fresh render or in-place update
// alike): none of this needs to persist across polls the way the board's
// cards do, so a plain innerHTML rebuild is simplest and matches the brief
// ("filter bar and side panels ... may remain full string-rebuild for now").
function _planChromeHtml(plan) {
  return `
    <div class="plan-header">
      <h2>${escapeHtml(plan.name)}</h2>
      ${plan.paused ? '<span class="badge" style="--badge-color: var(--c-parked)">paused</span>' : ""}
    </div>
    ${renderFilterBar(plan.stories)}
  `;
}

// The side panels — everything BELOW the card board (Notifications,
// Overlord decisions). Kept as a separate wholesale rebuild from
// _planChromeHtml so the two can sandwich the persistent board element in
// their original above/below positions instead of both landing before it.
function _notificationsPanelHtml(plan) {
  return `
    <div class="panel">
      <h3>Notifications</h3>
      <div class="panel-body">${_renderNotifications(plan.notification_records)}</div>
    </div>
  `;
}

function _decisionsPanelHtml(plan) {
  return `
    <div class="panel">
      <h3>Overlord decisions</h3>
      <div class="panel-body">${_renderDecisions(plan.decisions)}</div>
    </div>
  `;
}

function _planPanelsHtml(plan) {
  return `
    <div class="panels">
      ${_notificationsPanelHtml(plan)}
      ${_decisionsPanelHtml(plan)}
      <div class="panel" id="maturity-panel"
           data-plan="${escapeHtml(plan.name)}"></div>
    </div>
  `;
}

function renderPlanDetail(plan) {
  const section = document.getElementById("plan-detail");
  // The board's delegated click listener (attached once, below) can't close
  // over this call's `plan` — it fires on a later poll's click against a
  // listener bound on an earlier render — so it reads this module-level
  // reference instead, kept in sync on every render.
  currentPlanDetailData = plan;

  // Snapshot the detail section's state so we can restore it after
  // rebuilding the chrome: scroll position, and any focused filter chip.
  // Capturing a stable identity (data-dim + data-value) — rather than the
  // raw DOM node — lets us re-focus the matching chip if it survives.
  const snapshot = capturePlanDetailState(section);

  // Reuse the board from the previous render only when it's the SAME plan:
  // section.dataset.planName is stamped the first time a plan renders, and
  // survives across polls because (unlike before) we stop reassigning
  // section.innerHTML wholesale once a plan's board exists. Switching to a
  // different plan (or the very first render) still does a full teardown.
  const sameView = !!(section.dataset && section.dataset.planName === plan.name);
  let boardEl = (sameView && typeof section.querySelectorAll === "function")
    ? section.querySelectorAll(".board")[0]
    : null;

  if (boardEl) {
    const chromeEl = section.querySelectorAll(".plan-chrome")[0];
    if (chromeEl) chromeEl.innerHTML = _planChromeHtml(plan);
    const panelsEl = section.querySelectorAll(".plan-panels")[0];
    if (panelsEl) {
      if (diffNotificationsOnPoll) {
        // Poll-triggered same-plan update: keep the notifications panel body
        // intact so _diffNotificationsPanel (called from refresh()) can append
        // only new rows; only the decisions panel is rebuilt here.
        const decisionsPanel = panelsEl.querySelectorAll(".panel")[1];
        if (decisionsPanel) {
          // Rebuild only the decisions panel's inner content (heading + body),
          // not a full .panel wrapper, to avoid nesting a second bordered
          // .panel inside the existing one.
          decisionsPanel.innerHTML =
            '<h3>Overlord decisions</h3><div class="panel-body">' +
            _renderDecisions(plan.decisions) +
            '</div>';
        }
      } else {
        // First render or a deliberate user action (e.g. severity-filter
        // change): rebuild the whole panels, including notifications.
        panelsEl.innerHTML = _planPanelsHtml(plan);
      }
    }
    // Diffs the existing column-body elements in place (see
    // _tryUpdateBoardInPlace / _diffBoardCards) instead of rebuilding the
    // board as a string — this is what keeps unchanged `.card` nodes alive
    // (and their scroll/focus/selection) across a poll tick.
    renderBoard(plan.stories, boardEl);
  } else {
    if (section.dataset) section.dataset.planName = plan.name;
    section.innerHTML = `
      <div class="plan-chrome">${_planChromeHtml(plan)}</div>
      ${renderBoard(plan.stories)}
      <div class="plan-panels">${_planPanelsHtml(plan)}</div>
    `;
    boardEl = typeof section.querySelectorAll === "function"
      ? section.querySelectorAll(".board")[0]
      : null;
    // Delegated click listener: attached ONCE per fresh board (not per
    // card), since cards now persist across polls — attaching a fresh
    // listener to every `.card` on every render (the old approach) would
    // stack duplicate listeners on cards that survive a diff.
    if (boardEl && typeof boardEl.addEventListener === "function") {
      boardEl.addEventListener("click", (event) => {
        const target = event && event.target;
        const card = target && typeof target.closest === "function"
          ? target.closest(".card")
          : (target && target.classList && target.classList.contains("card") ? target : null);
        if (!card || !currentPlanDetailData) return;
        const key = card.dataset.key;
        _showStoryModal(
          currentPlanDetailData.name,
          currentPlanDetailData.stories[key],
          key,
          currentPlanDetailData.notification_records,
        );
      });
    }
  }

  section.querySelectorAll(".filter-chip").forEach((el) => {
    el.addEventListener("click", () => {
      const { dim, value } = el.dataset;
      if (dim === "sort") {
        state.filters.sort = value;
        saveFilters();
      } else {
        toggleFilter(dim, value);
      }
      updateHash();
      renderPlanDetail(plan);
    });
  });
  section.querySelectorAll('.filter-chip[data-dim="notif-severity"]').forEach((el) => {
    el.addEventListener("click", () => {
      _setNotifSeverityFilter(el.dataset.value);
      renderPlanDetail(plan);
    });
  });
  const searchInput = section.querySelector && section.querySelector(".filter-search");
  if (searchInput) {
    searchInput.addEventListener("input", () => {
      state.filters.search = searchInput.value;
      saveFilters();
      updateHash();
      renderPlanDetail(plan);
    });
  }
  const resetBtn = section.querySelector && section.querySelector(".filter-reset");
  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      state.filters = defaultFilters();
      saveFilters();
      updateHash();
      renderPlanDetail(plan);
    });
  }

  // Restore scroll + focus. Wrapped in try/catch defensively: a missing
  // focused element or a DOM that didn't survive the re-render must NOT throw.
  try {
    restorePlanDetailState(section, snapshot);
  } catch {
    /* snapshot stale or focus target gone; nothing to restore */
  }

  // Maturity panel: fetch on panel open/refresh (no polling loop of its own —
  // it re-renders with the rest of the plan detail on every refresh tick).
  // Fire-and-forget: endpoint failures collapse into an error row inside the
  // panel rather than failing the whole plan-detail render.
  const maturityPanel = section.querySelector
    && section.querySelector("#maturity-panel");
  if (maturityPanel) {
    renderMaturityPanel(maturityPanel, plan.name).catch(() => {});
  }
}

// Capture the scroll position and the focused element's identity from the
// detail section. The identity is just the data-dim + data-value pair of the
// focused filter chip (or null if nothing meaningful is focused). Plain data
// attributes survive innerHTML replacement, so we can look the chip back up
// in the new DOM.
function capturePlanDetailState(section) {
  if (!section) return { scrollTop: 0, focusKey: null };
  const ae = document.activeElement;
  let focusKey = null;
  if (ae && ae !== document.body && section.contains(ae) && ae.dataset
      && ae.dataset.dim !== undefined && ae.dataset.value !== undefined) {
    focusKey = `${ae.dataset.dim}\u0000${ae.dataset.value}`;
  }
  const snap = { scrollTop: section.scrollTop || 0, focusKey };
  // Capture search input state if it is focused. The `.filter-search` input is
  // an <input> element, so it carries a mutable `.value`; a focused filter chip
  // (which also exposes classList.contains) must not be mistaken for it.
  if (ae && ae !== document.body && section.contains(ae) && ae.classList
      && typeof ae.classList.contains === 'function'
      && ae.classList.contains('filter-search')
      && ae.value !== undefined) {
    snap.searchValue = ae.value;
    snap.searchSelectionStart = ae.selectionStart;
    snap.searchSelectionEnd = ae.selectionEnd;
  }
  return snap;
}

// Restore scrollTop, and re-focus the matching chip if it still exists.
// Negative/boundary case: focusKey is null (nothing was focused) or the chip
// with that identity was removed by the re-render — in both cases we simply
// skip focusing, never throw.
function restorePlanDetailState(section, snapshot) {
   if (!section || !snapshot) return;
   section.scrollTop = snapshot.scrollTop || 0;
   if (!snapshot.focusKey) {
     // restore search input if present
     if (snapshot.searchValue !== undefined) {
       try {
         const searchInput = section.querySelector('.filter-search');
         if (searchInput && typeof searchInput.focus === 'function') {
           searchInput.value = snapshot.searchValue;
           searchInput.focus();
           if (typeof searchInput.setSelectionRange === 'function') {
             const start = snapshot.searchSelectionStart !== undefined ? snapshot.searchSelectionStart : 0;
             const end = snapshot.searchSelectionEnd !== undefined ? snapshot.searchSelectionEnd : 0;
             searchInput.setSelectionRange(start, end);
           }
         }
       } catch {
         /* ignore errors restoring search input */
       }
     }
     return;
   }
   const [dim, value] = snapshot.focusKey.split("\u0000");
   const target = section.querySelector(
     `.filter-chip[data-dim="${CSS.escape(dim)}"][data-value="${CSS.escape(value)}"]`);
   if (target && typeof target.focus === "function") {
     target.focus();
   }
}

// Map of copy-button field name -> rendered value for the currently-open
// story modal. Populated by showStoryModal and consumed by handleCopyClick
// so the click handler can resolve the value without walking detached DOM
// (which the test fixtures simulate) or re-deriving from HTML.
const copyValues = new Map();

// Currently-open story in the modal: { plan, key } or null. Tracked so the
// async journal fetch (which races with the user opening a different story
// or closing the modal) can drop stale responses without racing in late
// DOM writes. Without this guard, a slow fetch from a prior open could
// overwrite the new story's journal section with the previous one.
const openStoryRef = { plan: null, key: null };

// Render a single timeline entry as HTML. Defensive defaults for fields the
// server may or may not have (entry missing `next_hint` must render nothing,
// never the literal string 'undefined'). Returns "" for entries with no
// step AND no summary so corrupt rows leave no visual artifact.
function renderJournalEntry(entry) {
  if (!entry || typeof entry !== "object") return "";
  const step = entry.step ? String(entry.step) : "";
  const summary = entry.summary ? String(entry.summary) : "";
  const nextHint = entry.next_hint ? String(entry.next_hint) : "";
  const ts = entry.ts ? String(entry.ts) : "";
  if (!step && !summary) return "";
  // next_hint is muted/caption-tone because it's metadata about what comes
  // NEXT (process hint), not part of the current entry's narrative. We omit
  // the block entirely when missing rather than emitting an empty <p>.
  const nextHtml = nextHint
    ? `<div class="timeline-next muted">next: ${escapeHtml(nextHint)}</div>`
    : "";
  const tsHtml = ts
    ? `<div class="timeline-ts muted">${escapeHtml(ts)}</div>`
    : "";
  return `
    <li class="timeline-item">
      <div class="timeline-step">${escapeHtml(step || "(no step)")}</div>
      <div class="timeline-summary">${escapeHtml(summary)}</div>
      ${nextHtml}
      ${tsHtml}
    </li>
  `;
}

// Render the entire Journal section for the story modal. `data` matches the
// /api/plans/{plan}/stories/{story}/journal response: {available, entries}.
// The empty state is shown when (a) the file is missing/malformed, or
// (b) the entries array is empty — both are visually indistinguishable in
// the UI, matching the endpoint's contract (test: empty list == unavailable).
function renderJournal(data) {
  if (!data || !data.available || !Array.isArray(data.entries) || data.entries.length === 0) {
    return `
      <h3 class="modal-section">Journal</h3>
      <p class="modal-empty" data-journal-empty>No journal yet.</p>
    `;
  }
  const items = data.entries.map(renderJournalEntry).filter(Boolean).join("");
  return `
    <h3 class="modal-section">Journal</h3>
    <ol class="timeline" data-journal-list>${items}</ol>
  `;
}

// Render the per-story Checklist section (DASHBOARD_STORY_PROGRESS_PLAN.md
// Tier 0): the tech-lead's ordered checklist (.agent_plan.md) and the
// executor's running scratchpad (.agent_scratchpad.md), both fetched from the
// story's worktree. `data` matches the /checklist endpoint response:
//   { plan: {available, text}, scratchpad: {available, text} }
//
// These artifacts only exist for stories run under PIPELINE_DECOMPOSE; every
// other story (the common case) gets the "No checklist" empty state. The
// agent writes both files, so their text is HTML-escaped (never trusted) —
// mirroring renderJournal's escaping contract.
function renderChecklist(data) {
  const empty = `
    <h3 class="modal-section">Checklist</h3>
    <p class="modal-empty" data-checklist-empty>No checklist (story not run with guided decomposition).</p>
  `;
  if (!data) return empty;
  const plan = (data.plan && typeof data.plan === "object") ? data.plan : {};
  const scratch = (data.scratchpad && typeof data.scratchpad === "object") ? data.scratchpad : {};
  if (!plan.available && !scratch.available) return empty;
  const parts = [`<h3 class="modal-section">Checklist</h3>`];
  if (plan.available) {
    parts.push(
      `<pre class="dsh-checklist-plan mono" data-checklist-plan>${escapeHtml(plan.text || "")}</pre>`
    );
  }
  if (scratch.available) {
    parts.push(`<h4 class="modal-subsection">Progress notes</h4>`);
    parts.push(
      `<pre class="dsh-checklist-scratch mono" data-checklist-scratch>${escapeHtml(scratch.text || "")}</pre>`
    );
  }
  return parts.join("");
}

// Fetch the journal for the currently-open story and inject the rendered
// HTML into the modal body. Guards against races with modal close /
// re-open by checking `openStoryRef` before mutating DOM. Failures (network
// down, server 500) are silently swallowed — the empty state shows by
// default. The journal is informational, not critical, so it must never
// block opening the modal or throw an unhandled promise rejection.
async function loadStoryJournal(plan, key, container) {
  if (!plan || !key || !container) return;
  openStoryRef.plan = plan;
  openStoryRef.key = key;
  try {
    const data = await fetchJson(
      `/api/plans/${encodeURIComponent(plan)}/stories/${encodeURIComponent(key)}/journal`
    );
    // Drop the response if the user closed the modal or opened a
    // different story in the meantime — late writes would overwrite
    // the new view with stale data.
    if (openStoryRef.plan !== plan || openStoryRef.key !== key) return;
    const html = renderJournal(data);
    const slot = container.querySelector("[data-journal-slot]");
    if (slot) slot.innerHTML = html;
  } catch {
    // 404 (story/plan gone) or transient network error: keep the empty
    // state placeholder. Don't rethrow — the modal stays usable.
    if (openStoryRef.plan !== plan || openStoryRef.key !== key) return;
    const slot = container.querySelector("[data-journal-slot]");
    if (slot) {
      slot.innerHTML = `
        <h3 class="modal-section">Journal</h3>
        <p class="modal-empty">No journal yet.</p>
      `;
    }
  }
}

// Fetch the checklist + scratchpad for the currently-open story and inject
// the rendered HTML into the modal's checklist slot. Same race-guard
// contract as loadStoryJournal (openStoryRef): a slow fetch that resolves
// after the user opened a different story is dropped, never overwriting the
// new view. Failures (network, 500) fall back to the empty state, never
// throw — the checklist is informational, like the journal.
async function loadStoryChecklist(plan, key, container) {
  if (!plan || !key || !container) return;
  openStoryRef.plan = plan;
  openStoryRef.key = key;
  try {
    const data = await fetchJson(
      `/api/plans/${encodeURIComponent(plan)}/stories/${encodeURIComponent(key)}/checklist`
    );
    if (openStoryRef.plan !== plan || openStoryRef.key !== key) return;
    const slot = container.querySelector("[data-checklist-slot]");
    if (slot) slot.innerHTML = renderChecklist(data);
  } catch {
    if (openStoryRef.plan !== plan || openStoryRef.key !== key) return;
    const slot = container.querySelector("[data-checklist-slot]");
    if (slot) slot.innerHTML = renderChecklist(null);
  }
}

// Build a small "copy" button. The data-copy attribute carries the field
// name; handleCopyClick resolves the value from `copyValues` so the markup
// stays a single tiny token and so copy click events for the currently-open
// story work consistently.
function renderCopyButton(fieldName, value) {
  copyValues.set(fieldName, String(value == null ? "" : value));
  return `<button type="button" class="copy-btn" data-copy="${escapeHtml(fieldName)}" aria-label="Copy ${escapeHtml(fieldName)}">copy</button>`;
}

// Click handler for `.copy-btn` elements within the story modal. Attached
// once at module load via event delegation; we don't rebind on every open.
// Silently swallows clipboard errors and environments where
// navigator.clipboard is undefined, so the button never throws.
function handleCopyClick(e) {
  const btn = e.target.closest && e.target.closest(".copy-btn");
  if (!btn) return;
  const field = btn.getAttribute("data-copy") || "";
  const text = copyValues.get(field) || "";
  if (!text) return;
  // navigator.clipboard requires a secure context; guard both the API
  // presence and the writeText call. In environments where the API is
  // unavailable (older browsers, insecure contexts, Node smoke tests)
  // the click is a silent no-op — never throws.
  if (typeof navigator === "undefined" || !navigator.clipboard
      || typeof navigator.clipboard.writeText !== "function") {
    return;
  }
  navigator.clipboard.writeText(text).then(
    () => {
      const prev = btn.textContent;
      btn.textContent = "copied";
      btn.classList.add("copied");
      setTimeout(() => {
        btn.textContent = prev;
        btn.classList.remove("copied");
      }, 1200);
    },
    () => {
      // Swallow rejection (denied permission, etc.) — feature detection
      // already passed so we treat this as a transient user-side issue.
    }
  );
}

export {
  renderPlanDetail, capturePlanDetailState, restorePlanDetailState,
  setDiffNotificationsOnPoll, resetPlanDetailState, initPlanDetail,
  renderJournal, renderJournalEntry, renderChecklist,
  loadStoryJournal, loadStoryChecklist,
  renderCopyButton, handleCopyClick, copyValues,
};
