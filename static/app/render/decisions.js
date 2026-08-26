import { escapeHtml } from "./board.js";

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

export { renderDecisions };
