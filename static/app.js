const STATUS_COLUMNS = [
  "todo", "in_progress", "tests_passed", "pr_open", "done",
  "changes_requested", "interrupted", "parked", "failed",
];

const state = {
  selectedPlan: null,
  pollHandle: null,
};

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
}

function renderPlanList(plans) {
  const nav = document.getElementById("plan-list");
  nav.innerHTML = "";
  for (const plan of plans) {
    const div = document.createElement("div");
    div.className = "plan-item" + (plan.name === state.selectedPlan ? " active" : "");
    const total = plan.story_count;
    const done = plan.status_counts.done || 0;
    div.innerHTML = `
      <div class="plan-name">${escapeHtml(plan.name)}</div>
      <div class="plan-meta">${done}/${total} done${plan.paused ? ' <span class="plan-paused">paused</span>' : ""}</div>
    `;
    div.addEventListener("click", () => selectPlan(plan.name));
    nav.appendChild(div);
  }
}

function renderBoard(stories) {
  const counts = {};
  for (const s of Object.values(stories)) {
    counts[s.status] = (counts[s.status] || 0) + 1;
  }

  const columns = STATUS_COLUMNS.map((status) => {
    const cards = Object.entries(stories)
      .filter(([, s]) => s.status === status)
      .map(([key, s]) => `
        <div class="card" style="--badge-color: var(--c-${status})" data-key="${escapeHtml(key)}">
          <div class="card-key">${escapeHtml(key)}</div>
          <div class="card-summary">${escapeHtml(s.summary || "(no summary)")}</div>
        </div>
      `).join("");
    return `
      <div class="column">
        <div class="column-header">
          <span>${status}</span>
          <span class="badge" style="--badge-color: var(--c-${status})">${counts[status] || 0}</span>
        </div>
        <div class="column-body">${cards}</div>
      </div>
    `;
  }).join("");

  return `<div class="board">${columns}</div>`;
}

function renderNotifications(lines) {
  if (!lines.length) return '<p class="empty-state">No notifications yet.</p>';
  return lines.slice().reverse()
    .map((line) => `<div class="log-line">${escapeHtml(line)}</div>`)
    .join("");
}

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

function renderPlanDetail(plan) {
  const section = document.getElementById("plan-detail");
  section.innerHTML = `
    <div class="plan-header">
      <h2>${escapeHtml(plan.name)}</h2>
      ${plan.paused ? '<span class="badge" style="--badge-color: var(--c-parked)">paused</span>' : ""}
    </div>
    ${renderBoard(plan.stories)}
    <div class="panels">
      <div class="panel">
        <h3>Notifications</h3>
        <div class="panel-body">${renderNotifications(plan.notifications)}</div>
      </div>
      <div class="panel">
        <h3>Overlord decisions</h3>
        <div class="panel-body">${renderDecisions(plan.decisions)}</div>
      </div>
    </div>
  `;

  section.querySelectorAll(".card").forEach((card) => {
    card.addEventListener("click", () => showStoryModal(plan.stories[card.dataset.key], card.dataset.key));
  });
}

function showStoryModal(story, key) {
  const modal = document.getElementById("story-modal");
  const body = document.getElementById("story-modal-body");
  const fields = [
    ["Key", key],
    ["Status", story.status],
    ["Summary", story.summary],
    ["Persona", story.persona],
    ["Model", story.model],
    ["Risk", story.risk],
    ["Dependencies", (story.dependencies || []).join(", ") || "(none)"],
    ["Worktree", story.worktree],
    ["PID", story.pid],
    ["PR URL", story.pr_url],
    ["Review verdict", story.review_verdict],
    ["Review feedback", story.review_feedback],
    ["Dispatch attempts", story.dispatch_attempts],
    ["Rework attempts", story.rework_attempts],
    ["Merge attempts", story.merge_attempts],
    ["Dispatch error", story.dispatch_error],
    ["Merge error", story.merge_error],
    ["Parked reason", story.parked_reason],
    ["Interrupted at", story.interrupted_at],
    ["Last commit", story.last_commit],
  ].filter(([, v]) => v !== undefined && v !== null && v !== "");

  body.innerHTML = `
    <h2>${escapeHtml(key)}</h2>
    <dl>
      ${fields.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`).join("")}
    </dl>
  `;
  modal.classList.remove("hidden");
}

function hideStoryModal() {
  document.getElementById("story-modal").classList.add("hidden");
}

async function selectPlan(name) {
  state.selectedPlan = name;
  await refresh();
}

async function refresh() {
  const { plans } = await fetchJson("/api/plans");
  renderPlanList(plans);

  if (state.selectedPlan) {
    try {
      const plan = await fetchJson(`/api/plans/${encodeURIComponent(state.selectedPlan)}`);
      renderPlanDetail(plan);
    } catch {
      state.selectedPlan = null;
    }
  }

  document.getElementById("last-updated").textContent =
    `updated ${new Date().toLocaleTimeString()}`;
}

function startPolling() {
  if (state.pollHandle) clearInterval(state.pollHandle);
  state.pollHandle = setInterval(refresh, 4000);
}

function stopPolling() {
  if (state.pollHandle) clearInterval(state.pollHandle);
  state.pollHandle = null;
}

document.getElementById("story-modal-close").addEventListener("click", hideStoryModal);
document.getElementById("story-modal").addEventListener("click", (e) => {
  if (e.target.id === "story-modal") hideStoryModal();
});
document.getElementById("auto-refresh").addEventListener("change", (e) => {
  e.target.checked ? startPolling() : stopPolling();
});

refresh();
startPolling();
