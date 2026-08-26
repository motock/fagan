import { escapeHtml } from "./render/board.js";

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

export { renderUsage };
