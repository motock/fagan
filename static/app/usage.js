import { escapeHtml } from "./render/board.js";

function renderUsage(usage) {
  const banner = document.getElementById("usage-banner");
  const backends = usage && Array.isArray(usage.backends) ? usage.backends : [];
  if (!usage || (!usage.available && backends.length === 0)) {
    banner.classList.add("hidden");
    return;
  }
  banner.classList.remove("hidden");
  let html;
  if (usage.gate_blind) {
    // Failing open: spend is unguarded. Make it impossible to miss.
    banner.classList.add("blind");
    const fails = usage.consecutive_parse_failures || 0;
    html =
      `&#9888; <strong>Claude usage gate is BLIND</strong> — the usage probe has ` +
      `been unparseable (${fails} consecutive failures) since ` +
      `${escapeHtml(usage.blind_since || "?")}, so the gate is failing OPEN and ` +
      `spend is unguarded. Check the <code>claude -p /cost</code> output / poller.`;
  } else if (usage.available) {
    banner.classList.remove("blind");
    const s = usage.session_pct, w = usage.week_pct;
    const paused = usage.paused ? " &middot; <strong>PAUSED</strong>" : "";
    html =
      `Usage gate: session ${s}% &middot; week ${w}%${paused} ` +
      `<span class="muted">(measured ${escapeHtml(usage.measured_at || "?")})</span>`;
  } else {
    // Provider-neutral install: no Claude usage state, but backend rows exist.
    banner.classList.remove("blind");
    html = "";
  }
  if (backends.length > 0) {
    html += renderBackends(backends);
  }
  banner.innerHTML = html;
}

// One line per provider (first-seen order), listing each role with an
// ok/blocked indicator and the driver-generated reason when blocked.
function renderBackends(backends) {
  const groups = new Map();
  for (const row of backends) {
    if (!row) continue;
    const key = String(row.provider ?? "?");
    if (!groups.has(key)) groups.set(key, { model: row.model, roles: [] });
    groups.get(key).roles.push(row);
  }
  let html = "";
  for (const [provider, group] of groups) {
    const label = group.model
      ? `${escapeHtml(provider)}/${escapeHtml(group.model)}`
      : escapeHtml(provider);
    const roles = group.roles
      .map((row) =>
        row.ok
          ? `&#10003; ${escapeHtml(row.role || "?")}`
          : `&#10007; ${escapeHtml(row.role || "?")}` +
            (row.reason ? ` — ${escapeHtml(row.reason)}` : ""),
      )
      .join(", ");
    html += `<div class="muted">${label}: ${roles}</div>`;
  }
  return html;
}

export { renderUsage };