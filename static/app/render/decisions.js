import { escapeHtml } from "./board.js";

// A field is "present" when it carries real content: not undefined, not null
// and not blank once stringified. Used instead of truthiness so that values
// like `tier: 0` survive while `tier: ""` is dropped.
function _present(v) {
  return v !== undefined && v !== null && String(v).trim() !== "";
}

// The decisions log holds two record shapes: plan-approval records (status /
// role / decided_by / at, no `question`) and overlord rulings (question /
// ruling / decided_at). Render each shape with the fields it actually has and
// never print a placeholder for a missing one.
function _decisionView(record) {
  const r = record !== null && typeof record === "object" ? record : {};
  const when = _present(r.decided_at) ? r.decided_at : _present(r.at) ? r.at : "";
  const meta = [];

  if (typeof r.question === "string" && r.question.trim() !== "") {
    const title = r.question;
    if (_present(r.ruling)) meta.push(String(r.ruling));
    if (_present(r.tier)) meta.push(`tier=${r.tier}`);
    if (_present(r.risk)) meta.push(`risk=${r.risk}`);
    if (_present(when)) meta.push(String(when));
    return { title, meta };
  }

  if (typeof r.status === "string" && r.status.trim() !== "") {
    const title = `Plan ${r.status}`;
    const who = _present(r.role) ? r.role : _present(r.decided_by) ? r.decided_by : "";
    if (_present(who)) {
      const selfApproved = r.self_approved === true ? " (self-approved)" : "";
      meta.push(`by ${who}${selfApproved}`);
    }
    if (_present(when)) meta.push(String(when));
    return { title, meta };
  }

  if (_present(when)) meta.push(String(when));
  return { title: "Decision recorded", meta };
}

function renderDecisions(decisions) {
  if (!decisions.length) return '<p class="empty-state">No decisions logged yet.</p>';
  return decisions.slice().reverse().map((d) => {
    const { title, meta } = _decisionView(d);
    return `
    <div class="decision">
      <div class="decision-q">${escapeHtml(title)}</div>
      <div class="decision-meta">${escapeHtml(meta.join(" · "))}</div>
    </div>
  `;
  }).join("");
}

export { renderDecisions };
