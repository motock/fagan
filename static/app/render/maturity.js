// Maturity panel renderer (a3-maturity-metrics).
//
// Renders two client-side sections from the maturity endpoints:
//   - GET /api/plans/<plan>/metrics  -> per-plan cost metrics
//   - GET /api/guard-liveness        -> guard-liveness status
//
// Pure string builders (no DOM writes) so node-based tests can assert on the
// markup; the DOM write lives in renderMaturityPanel().

import { escapeHtml } from "./board.js";
import { fetchPlanMetrics, fetchGuardLiveness } from "../api.js";

export const METRICS_ENDPOINT = "/api/plans/{plan}/metrics";
export const GUARD_LIVENESS_ENDPOINT = "/api/guard-liveness";

// ---------------------------------------------------------------------------
// Plan metrics section
// ---------------------------------------------------------------------------

// Sort stories by rework cycles descending — the expensive stories on top.
// Ties fall back to story_key ascending for a stable, readable order.
function _sortStoriesByReworkDesc(stories) {
  return (stories || []).slice().sort((a, b) => {
    const reworkA = a && a.rework_cycles ? a.rework_cycles : 0;
    const reworkB = b && b.rework_cycles ? b.rework_cycles : 0;
    if (reworkB !== reworkA) return reworkB - reworkA;
    const keyA = (a && a.story_key) || "";
    const keyB = (b && b.story_key) || "";
    return keyA < keyB ? -1 : keyA > keyB ? 1 : 0;
  });
}

function _metricsRow(story) {
  const key = escapeHtml((story && story.story_key) || "?");
  const merged = story && story.merged ? "yes" : "no";
  const rework = story && story.rework_cycles ? story.rework_cycles : 0;
  const escalations = story && story.escalations ? story.escalations : 0;
  return (
    `<tr class="maturity-story-row">`
    + `<td class="maturity-story-key">${key}</td>`
    + `<td class="maturity-story-merged">${merged}</td>`
    + `<td class="maturity-story-rework">${rework}</td>`
    + `<td class="maturity-story-escalations">${escalations}</td>`
    + `</tr>`
  );
}

function _metricsSection(metrics) {
  if (!metrics || typeof metrics !== "object") {
    return `<p class="maturity-error">Failed to load plan metrics.</p>`;
  }
  const rollup = metrics.rollup || {};
  const stories = _sortStoriesByReworkDesc(metrics.stories);
  if (!stories.length) {
    return `<p class="maturity-empty">no notification data for this plan</p>`;
  }
  const costPerMerged = rollup.cost_per_merged_story;
  const costDisplay = costPerMerged === null || costPerMerged === undefined
    ? "-"
    : escapeHtml(String(costPerMerged));
  return `
    <div class="maturity-rollup">
      <span class="maturity-rollup-stories-merged">${rollup.stories_merged ?? 0}</span>
      <span class="maturity-rollup-rework">${rollup.total_rework_cycles ?? 0}</span>
      <span class="maturity-rollup-escalations">${rollup.total_escalations ?? 0}</span>
      <span class="maturity-rollup-cost">${costDisplay}</span>
    </div>
    <table class="maturity-stories">
      <thead>
        <tr>
          <th>story</th><th>merged</th><th>rework</th><th>escalations</th>
        </tr>
      </thead>
      <tbody>
        ${stories.map(_metricsRow).join("")}
      </tbody>
    </table>
  `;
}

// ---------------------------------------------------------------------------
// Guard-liveness section
// ---------------------------------------------------------------------------

function _recurrenceAlerts(summary, entries) {
  const alertCount = summary && summary.recurrence_alerts ? summary.recurrence_alerts : 0;
  if (!alertCount) return "";
  const rows = (entries || [])
    .filter((e) => e && ((e.missing || []).length + (e.uncollected || []).length) > 0)
    .map((e) => {
      const files = (e.missing || []).concat(e.uncollected || []);
      return (
        `<li class="maturity-alert-item">`
        + `<span class="maturity-alert-mode">${escapeHtml(String(e.mode ?? ""))}</span>`
        + `<span class="maturity-alert-reason">${escapeHtml(String(e.reason || "guard files not live"))}</span>`
        + `<span class="maturity-alert-files">${escapeHtml(files.join(", "))}</span>`
        + `</li>`
      );
    })
    .join("");
  return `<ul class="maturity-alerts">${rows}</ul>`;
}

function _guardSection(guard) {
  if (!guard || typeof guard !== "object") {
    return `<p class="maturity-error">Failed to load guard liveness.</p>`;
  }
  if (guard.dataset_found === false) {
    return `<p class="maturity-muted">failure-mode dataset not found</p>`;
  }
  const summary = guard.summary || {};
  const withGuard = summary.with_guard ?? 0;
  const total = summary.total ?? 0;
  const missing = summary.missing_files ?? 0;
  const uncollected = summary.uncollected_files ?? 0;
  const healthy = missing === 0 && uncollected === 0;
  const summaryClass = healthy ? "maturity-summary-ok" : "maturity-summary-warn";
  return `
    <p class="maturity-liveness-summary ${summaryClass}">${withGuard}/${total} modes cite a guard; ${missing} missing, ${uncollected} uncollected</p>
    ${_recurrenceAlerts(summary, guard.entries)}
  `;
}

// ---------------------------------------------------------------------------
// Panel assembly
// ---------------------------------------------------------------------------

function _errorRow(message) {
  return `<details class="maturity-error-row"><summary>Maturity data unavailable</summary>`
    + `<p class="maturity-error-detail">${escapeHtml(message)}</p></details>`;
}

// Fetch both endpoints and render into the given container element. Collapses
// endpoint failures (404/500/network) into an error row instead of a blank
// panel. Returns a promise resolving to the rendered HTML string.
export async function renderMaturityPanel(container, plan) {
  const target = container || document.getElementById("maturity-panel");
  if (!target) return "";
  const results = await Promise.allSettled([
    fetchPlanMetrics(plan),
    fetchGuardLiveness(),
  ]);
  const [metricsRes, guardRes] = results;
  const metrics = metricsRes.status === "fulfilled" ? metricsRes.value : null;
  const guard = guardRes.status === "fulfilled" ? guardRes.value : null;
  const errors = [];
  if (metricsRes.status === "rejected") {
    errors.push(`plan metrics: ${metricsRes.reason && metricsRes.reason.message}`);
  }
  if (guardRes.status === "rejected") {
    errors.push(`guard liveness: ${guardRes.reason && guardRes.reason.message}`);
  }
  const html = `
    <div class="maturity-panel">
      <h3 class="maturity-heading">Maturity</h3>
      ${errors.length ? errors.map(_errorRow).join("") : ""}
      <div class="maturity-metrics">${_metricsSection(metrics)}</div>
      <div class="maturity-guard">${_guardSection(guard)}</div>
    </div>
  `;
  target.innerHTML = html;
  return html;
}