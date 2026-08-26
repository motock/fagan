import { STATUS_COLUMNS } from "../state.js";
import { escapeHtml } from "./board.js";

// Format a fractional rate (0..1, or 0.0 from the backend when dispatched
// count is zero) as a percentage string. We never want to render "NaN%" or
// "undefined%" — a denominator of 0 must still produce "0%".
function fmtPct(rate) {
  const v = Number(rate);
  if (!Number.isFinite(v)) return "0%";
  return `${Math.round(v * 100)}%`;
}

// Keyed-diff state for _diffOverviewPlanRows: maps each plan name to its
// live `.overview-plan-row` DOM node, mirroring plan-list.js's
// planListRowsByName. renderOverview rebuilds the rest of the section's
// markup (including a fresh empty `.overview-plan-list` <ul>) every poll
// tick, so this map - not the <ul>'s own children - is the only thing that
// lets a row survive across ticks: existing rows are moved (not recreated)
// into the new <ul>.
let overviewPlanRowsByName = new Map();

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

export {
  fmtPct,
  renderOverview,
  _overviewPlanMetaMarkup,
  _buildOverviewPlanRow,
  _diffOverviewPlanRows,
};
