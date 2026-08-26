import { chip, escapeHtml } from "./board.js";

// notifications.js cannot statically `import ... from "../../app.js"`: app.js's
// dynamic-import test harness cache-busts its own URL with a `?t=` query
// param, so a static back-import here would resolve to a SECOND, separate
// app.js module instance and re-run its top-level init (polling, DOM wiring)
// out of order (see plan-list.js's initPlanList for the same pattern).
// app.js instead calls initNotifications() once at init with its own
// function reference, breaking the import-graph cycle while keeping
// identical call-time behavior.
let _selectComms;
function initNotifications({ selectComms }) {
  _selectComms = selectComms;
}

const NOTIF_SEVERITY_COLOR = { "error": "--c-failed", "warning": "--c-parked", "info": "--c-unknown" };

function notificationKey(rec) {
  return rec.dedup_key || (rec.ts + '-' + rec.message);
}

function pickNewNotifications(plans, seenMap) {
  const newNotifs = [];
  for (const plan of plans) {
    const rec = plan.latest_notification;
    if (!rec) continue;
    const key = notificationKey(rec);
    const seen = seenMap.get(plan.name);
    if (seen !== key) {
      newNotifs.push({ plan, record: rec });
    }
  }
  return newNotifs;
}

// Push a toast to stack
function pushToast({ severity, planName, storyKey, message }) {
  const stack = document.getElementById("toast-stack");
  if (!stack) return;
  const node = document.createElement("div");
  node.className = "toast";
  const stripe = NOTIF_SEVERITY_COLOR[severity] || NOTIF_SEVERITY_COLOR["info"];
  node.style.setProperty("--stripe", stripe);
  node.innerHTML = `
    <div class="toast-row">
      <span class="toast-key">${escapeHtml(planName)}</span>
      <span class="toast-msg">${escapeHtml(message)}</span>
      <button class="toast-dismiss" aria-label="Dismiss">✕</button>
    </div>
    <div class="toast-actions"></div>
  `;
  const dismiss = node.querySelector(".toast-dismiss");
  dismiss.addEventListener("click", () => node.remove());
  const actions = node.querySelector(".toast-actions");
  if (severity === "error" || severity === "warning") {
    const ask = document.createElement("button");
    ask.className = "toast-ask";
    ask.textContent = "Ask";
    ask.addEventListener("click", () => {
      _selectComms();
      const input = document.getElementById("comms-input");
      if (input) {
        input.value = `${storyKey} ${severity === "error" ? "failed" : "is parked"} - what happened?`;
        input.focus();
      }
      node.remove();
    });
    actions.appendChild(ask);
  } else {
    setTimeout(() => node.remove(), 6000);
  }
  stack.appendChild(node);
}

let notifSeverityFilter = "all";
// plan-detail.js's renderPlanDetail can't reassign this module's `let`
// binding directly (ES module bindings are read-only to importers), so it
// calls this setter instead when the notif-severity filter chip is clicked.
function setNotifSeverityFilter(value) {
  notifSeverityFilter = value;
}

function filterNotifications(records, severity) {
  if (!records) return [];
  if (!severity || severity === "all") return records.slice();
  return records.filter((r) => (r && r.severity) === severity);
}

function decodeHtmlEntities(s) {
  return String(s).replace(/&(amp|lt|gt|quot|#39);/g, (m, name) => ({
    "amp": "&", "lt": "<", "gt": ">", "quot": '"', "#39": "'",
  }[name]));
}

function renderNotifications(records) {
  const chipRow = `<div class="filter-group"><span class="filter-group-label">Severity</span>${
    ["all", "error", "warning", "info"]
      .map((sev) => chip("notif-severity", sev, sev, notifSeverityFilter === sev))
      .join("")
  }</div>`;
  const filtered = filterNotifications(records, notifSeverityFilter);
  if (!filtered.length) {
    const emptyMsg = (records && records.length)
      ? "No notifications at this severity."
      : "No notifications yet.";
    return chipRow + `<p class="empty-state">${emptyMsg}</p>`;
  }
  return chipRow + filtered.reverse().map(function (r) {
    var color = NOTIF_SEVERITY_COLOR[r.severity] || "--c-unknown";
    var sev = escapeHtml(r.severity || "");
    var parts = [];
    parts.push('<div class="log-line" data-dedup-key="' + escapeHtml(r.dedup_key || (r.ts + '-' + r.message)) + '">');
    parts.push('<span class="badge" style="--badge-color: var(' + color + ')">' + sev + '</span>');
    if (r.story_key) {
      parts.push('<span class="mono">' + escapeHtml(r.story_key) + '</span>');
    }
    parts.push(escapeHtml(r.message || ""));
    if (r.count && r.count > 1) {
      parts.push('<span class="badge">x' + escapeHtml(String(r.count)) + '</span>');
    }
    if (r.ts) {
      parts.push(escapeHtml(r.ts));
    }
    parts.push('</div>');
    return parts.join(' ');
  }).join('');
}

// Incrementally append new notification rows based on dedup_key
function _diffNotificationsPanel(panelBodyEl, records) {
  if (!panelBodyEl || !records) return;
  const existing = new Set(
    Array.from(panelBodyEl.querySelectorAll('[data-dedup-key]')).map(
      (el) => decodeHtmlEntities(el.getAttribute('data-dedup-key'))
    )
  );
  for (const r of records) {
    const key = r.dedup_key || (r.ts + '-' + r.message);
    if (existing.has(key)) continue;
    const tmp = document.createElement('div');
    tmp.innerHTML = renderNotifications([r]);
    const node = tmp.querySelector('.log-line');
    if (node) panelBodyEl.appendChild(node);
  }
}

export {
  initNotifications,
  NOTIF_SEVERITY_COLOR,
  pickNewNotifications,
  pushToast,
  notifSeverityFilter,
  setNotifSeverityFilter,
  filterNotifications,
  decodeHtmlEntities,
  renderNotifications,
  _diffNotificationsPanel,
  notificationKey,
};
