import { state, STATUS_COLUMNS, SORT_OPTIONS, BACKEND_VALUES, ESCALATED_VALUES } from "../state.js";

const RISK_RANK = { high: 3, medium: 2, low: 1 };

// Stale threshold (minutes) for the in_progress "aged" indicator on cards.
// Mirrors STALE_IN_PROGRESS_MINUTES in dashboard.py — the dashboard hands
// us `last_activity` and the UI does the math so cards stay accurate
// without re-fetching.
const STALE_IN_PROGRESS_MINUTES = 30;

// Parse an ISO-8601 string into a Date. Returns null for any falsy or
// unparseable value — the dashboard never promises strict formatting, and
// a bad row should just hide the age label rather than throw.
function parseIso(ts) {
  if (typeof ts !== "string" || !ts) return null;
  // Date.parse accepts the trailing 'Z' suffix natively, so a direct call
  // handles both "...+00:00" and "...Z" forms. Any garbage falls through
  // to NaN and we return null.
  const d = new Date(ts);
  if (isNaN(d.getTime())) return null;
  return d;
}

// Format a positive (or zero) age in seconds as a short relative-age label
// like "just now" (<60s), "3m ago" (<1h), "2h ago" (<1d), "4d ago".
// Future timestamps (negative age) are clamped to "just now" — never show
// negative durations.
function relativeAgeLabel(ageSeconds) {
  if (ageSeconds < 60) return "just now";
  const minutes = Math.floor(ageSeconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

// Compute the human-readable age for a story's last_activity ISO timestamp,
// or null if there is no last_activity. Negative ages (future timestamps)
// are clamped to "just now".
function ageLabelFor(lastActivity) {
  const d = parseIso(lastActivity);
  if (!d) return null;
  const ageSec = (Date.now() - d.getTime()) / 1000;
  return relativeAgeLabel(ageSec);
}

// True when an in_progress story has a last_activity older than the stale
// threshold. Stories without last_activity, or with other statuses, are
// never stale here — we only warn about agents that *were* running and
// appear to have stopped.
function isStaleInProgress(story) {
  if (!story || story.status !== "in_progress") return false;
  const d = parseIso(story.last_activity);
  if (!d) return false;
  const ageMin = (Date.now() - d.getTime()) / 60000;
  return ageMin > STALE_IN_PROGRESS_MINUTES;
}

// True only when the SERVER has attached a wedge verdict to the story
// (story.wedge.wedged === true, set by GET /api/plans/{plan} for in_progress
// stories). This deliberately does NOT re-derive staleness client-side —
// last_activity aging stays isStaleInProgress's job and the `.stale` class
// stays its signal. The two indicators must not be conflated: an aged story
// without a server wedge verdict is not wedged. The status guard mirrors
// isStaleInProgress's shape so a done story still carrying a stale wedge
// object is not flagged.
function isWedged(story) {
  if (!story || story.status !== "in_progress") return false;
  return Boolean(story.wedge && story.wedge.wedged === true);
}

// Build the wedged badge markup for a story the server flagged as wedged.
// Returns "" for anything else, so callers can push unconditionally. The
// reasons list is HTML-escaped because it is interpolated into a title
// attribute rendered via innerHTML.
function wedgedBadgeHtml(story) {
  if (!isWedged(story)) return "";
  const rawReasons = story.wedge && Array.isArray(story.wedge.reasons)
    ? story.wedge.reasons
    : [];
  const reasons = rawReasons.map((r) => escapeHtml(String(r))).join(", ");
  return `<span class="card-badge card-badge-wedged" title="Wedged: ${reasons}">wedged</span>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function riskRank(story) {
  return RISK_RANK[story.risk] || 0;
}

function activityScore(story) {
  return (story.dispatch_attempts || 0)
    + (story.rework_attempts || 0)
    + (story.merge_attempts || 0);
}

// Persona/risk/backend/escalation-filter the cards of one status column,
// then sort per state.filters.sort. Status filtering happens at the column
// level in renderBoard, not here.
function applyFilters(entries) {
  const { personas, risks, backends, escalated, sort, search } = state.filters;
  const term = (search || "").trim().toLowerCase();
  // A story without a `backend` field is treated as "local" (the orchestrator
  // hasn't chosen anything else yet). This must agree with the badge logic
  // in renderBoard so a missing backend never leaks as "undefined".
  const storyBackend = (s) => (s && s.backend) ? s.backend : "local";
  const storyEscalated = (s) => !!(s && s.escalated);
  const filtered = entries.filter(([key, s]) =>
    (personas.length === 0 || personas.includes(s.persona))
    && (risks.length === 0 || risks.includes(s.risk))
    && (backends.length === 0 || backends.includes(storyBackend(s)))
    && (escalated.length === 0
        || (escalated.includes("yes") && storyEscalated(s))
        || (escalated.includes("no") && !storyEscalated(s)))
    && (term === "" || (String(key) + " " + (s.summary || "")).toLowerCase().includes(term)));

  const comparators = {
    key: ([a], [b]) => a.localeCompare(b, undefined, { numeric: true }),
    risk: ([, a], [, b]) => riskRank(b) - riskRank(a),
    activity: ([, a], [, b]) => activityScore(b) - activityScore(a),
  };
  filtered.sort(comparators[sort] || comparators.key);
  return filtered;
}

// `existingBoardEl`, when given, is a live `.board` element from a PRIOR
// renderPlanDetail call for the SAME plan. In that mode renderBoard updates
// the columns already inside it in place (see _tryUpdateBoardInPlace) —
// keyed by data-key via _diffBoardCards — instead of tearing the whole
// board down and rebuilding it as an HTML string, so unchanged `.card`
// nodes survive a poll tick (no flicker, no lost scroll/focus/selection).
// Called with a single argument (no live board yet — first render of a
// plan, or the set of visible status columns just changed), it falls back
// to the original full string-build path and returns markup for the
// caller to assign via innerHTML.
function renderBoard(stories, existingBoardEl) {
  // Total story count for the whole plan — used by the done column's
  // completion hint so the user sees "2/5 done" instead of just "2",
  // without changing the filter logic (we still count filtered cards
  // inside each column). Computed once per render to avoid repeating the
  // work in every column map.
  const planTotal = Object.keys(stories).length;

  const statuses = state.filters.statuses
    .filter((status) => STATUS_COLUMNS.includes(status))
    .sort((a, b) => STATUS_COLUMNS.indexOf(a) - STATUS_COLUMNS.indexOf(b));

  if (existingBoardEl && _tryUpdateBoardInPlace(existingBoardEl, statuses, stories, planTotal)) {
    return null;
  }

  const columns = statuses.map((status) => {
    const entries = applyFilters(
      Object.entries(stories).filter(([, s]) => s.status === status));
    const cards = entries.map(([key, s]) => {
      // Per-card decorations: age label when last_activity exists, and
      // a stale class on aged in_progress stories so the user can see
      // at a glance which agents are wedged. Both are derived from
      // story.last_activity (server-supplied) using client-side time
      // so they stay correct between polls.
      const ageLabel = ageLabelFor(s.last_activity);
      const stale = isStaleInProgress(s);
      const classes = ["card"];
      if (stale) classes.push("stale");
      // Backend / escalation badges. A story without a `backend` field
      // is treated as local and gets no claude badge; a non-escalated
      // story gets no escalated badge. Keeping these conditional means
      // most cards stay visually quiet — the badges surface the
      // *interesting* cases (claude-bound, escalated) rather than
      // repeating what the status stripe already conveys.
      const badges = [];
      if (s.backend === "claude") {
        badges.push(`<span class="card-badge card-badge-claude" title="Backend: claude">claude</span>`);
      }
      if (s.escalated) {
        badges.push(`<span class="card-badge card-badge-escalated" title="Escalated to claude">escalated</span>`);
      }
      const wedgedBadge = wedgedBadgeHtml(s);
      if (wedgedBadge) {
        badges.push(wedgedBadge);
        classes.push("wedged");
      }
      const badgesHtml = badges.length
        ? `<div class="card-badges">${badges.join("")}</div>`
        : "";
      // Progress bar for in_progress stories with checklist data
      const pct = (s.status === "in_progress" && s.progress && s.progress.total > 0)
        ? Math.round(s.progress.done / s.progress.total * 100) : 0;
      const progressHtml = (s.status === "in_progress" && s.progress && s.progress.total > 0)
        ? `<div class="card-progress">
             <div class="card-progress-track"><div class="card-progress-fill" style="width: ${pct}%"></div></div>
             <span class="card-progress-label">${s.progress.done}/${s.progress.total}</span>
           </div>`
        : "";
      return `
      <div class="${classes.join(" ")}" style="--badge-color: var(--c-${status})" data-key="${escapeHtml(key)}">
        <div class="card-key">${escapeHtml(key)}</div>
        <div class="card-summary">${escapeHtml(s.summary || "(no summary)")}</div>
        ${badgesHtml}${progressHtml}
        ${ageLabel ? `<div class="card-age${stale ? " stale" : ""}">${escapeHtml(ageLabel)}</div>` : ""}
      </div>
`;
    }).join("");
    // Thin completion hint for the done column: shows done / plan-total
    // so the user sees plan-wide progress at a glance. We use planTotal
    // (not entries.length) so the fraction stays meaningful even when
    // persona/risk filters narrow the visible cards. Hidden when the
    // plan has no stories yet to avoid "0/0".
    const completion = (status === "done" && planTotal > 0)
      ? `<div class="column-completion">${entries.length}/${planTotal}</div>`
      : "";
    return `
      <div class="column" data-status="${escapeHtml(status)}">
        <div class="column-header">
          <span>${status}</span>
          <span class="badge" style="--badge-color: var(--c-${status})">${entries.length}</span>
        </div>
        ${completion}
        <div class="column-body">${cards}</div>
      </div>
    `;
  }).join("");

  let inner;
  if (!columns) {
    inner = '<p class="empty-state">No statuses selected.</p>';
  } else {
    const term = (state.filters.search || "").trim();
    const anyCards = columns.includes('data-key="');
    inner = (term && !anyCards)
      ? `<div class="board-empty">No matches for "${escapeHtml(term)}"</div>`
      : columns;
  }

  if (existingBoardEl) {
    existingBoardEl.innerHTML = inner;
    return null;
  }
  return `<div class="board">${inner}</div>`;
}

// In-place update path for renderBoard: reuses the `.column` elements
// already inside `existingBoardEl` when the set of visible status columns
// is unchanged from the last render, updating each column's count badge
// and completion hint in place and diffing its cards via _diffBoardCards
// (keyed by data-key, so unchanged cards are never torn down). Returns
// false — doing nothing — when the column set changed (a status filter was
// toggled, or this is the very first populated render), leaving the caller
// to fall back to a full rebuild; that's a rare, user-triggered event, not
// the steady poll-tick case this diff targets.
function _tryUpdateBoardInPlace(existingBoardEl, statuses, stories, planTotal) {
  const currentColumns = Array.from(existingBoardEl.querySelectorAll(".column"));
  const currentStatuses = currentColumns.map((el) => el.dataset.status);
  const sameColumnSet = currentStatuses.length === statuses.length
    && currentStatuses.length > 0
    && currentStatuses.every((s, i) => s === statuses[i]);
  if (!sameColumnSet) return false;

  for (let i = 0; i < statuses.length; i++) {
    const status = statuses[i];
    const columnEl = currentColumns[i];
    const entries = applyFilters(
      Object.entries(stories).filter(([, s]) => s.status === status));

    const badgeEl = columnEl.querySelectorAll(".badge")[0];
    if (badgeEl) {
      badgeEl.textContent = String(entries.length);
      badgeEl.innerHTML = String(entries.length);
    }

    let completionEl = columnEl.querySelectorAll(".column-completion")[0];
    if (status === "done" && planTotal > 0) {
      const label = `${entries.length}/${planTotal}`;
      if (completionEl) {
        completionEl.textContent = label;
        completionEl.innerHTML = label;
      } else {
        completionEl = document.createElement("div");
        completionEl.className = "column-completion";
        completionEl.textContent = label;
        completionEl.innerHTML = label;
        const bodyEl = columnEl.querySelectorAll(".column-body")[0];
        if (bodyEl && typeof columnEl.insertBefore === "function") {
          columnEl.insertBefore(completionEl, bodyEl);
        } else if (typeof columnEl.appendChild === "function") {
          columnEl.appendChild(completionEl);
        }
      }
    } else if (completionEl && typeof completionEl.remove === "function") {
      completionEl.remove();
    }

    const bodyEl = columnEl.querySelectorAll(".column-body")[0];
    if (bodyEl) _diffBoardCards(bodyEl, entries);
  }
  return true;
}

// Diffable card rendering for a single column body.
// `columnBodyEl` is the `.column-body` element.
// `storiesForColumn` is an array of [key, story] tuples, already sorted.
function _diffBoardCards(columnBodyEl, storiesForColumn) {
  // Map existing cards by data-key.
const existing = Array.from(columnBodyEl.querySelectorAll('.card')).reduce((m, el) => {
    m[el.dataset.key] = el;
    return m;
  }, {});
  const newKeys = new Set();
  for (const [key, s] of storiesForColumn) {
    const status = s.status;
    const ageLabel = ageLabelFor(s.last_activity);
    const stale = isStaleInProgress(s);
    const classes = ['card'];
    if (stale) classes.push('stale');
    const badges = [];
    if (s.backend === 'claude') {
      badges.push(`<span class="card-badge card-badge-claude" title="Backend: claude">claude</span>`);
    }
    if (s.escalated) {
      badges.push(`<span class="card-badge card-badge-escalated" title="Escalated to claude">escalated</span>`);
    }
    const wedgedBadge = wedgedBadgeHtml(s);
    if (wedgedBadge) {
      badges.push(wedgedBadge);
      classes.push('wedged');
    }
    const badgesHtml = badges.length
      ? `<div class="card-badges">${badges.join('')}</div>`
      : '';
    const pct = (s.status === 'in_progress' && s.progress && s.progress.total > 0)
      ? Math.round(s.progress.done / s.progress.total * 100)
      : 0;
    const progressHtml = (s.status === 'in_progress' && s.progress && s.progress.total > 0)
      ? `<div class="card-progress"><div class="card-progress-track"><div class="card-progress-fill" style="width: ${pct}%"></div></div><span class="card-progress-label">${s.progress.done}/${s.progress.total}</span></div>`
      : '';
     const cardInnerHtml = `
           <div class="card-key">${escapeHtml(key)}</div>
           <div class="card-summary">${escapeHtml(s.summary || '(no summary)')}</div>
           ${badgesHtml}${progressHtml}
           ${ageLabel ? `<div class="card-age${stale ? ' stale' : ''}">${escapeHtml(ageLabel)}</div>` : ''}`;
     let cardEl = existing[key];
     if (!cardEl) {
       cardEl = document.createElement('div');
       cardEl.dataset.key = key;
     }
     // Refresh outer node's class/style
     cardEl.className = classes.join(' ');
     cardEl.style.setProperty('--badge-color', `var(--c-${status})`);
     cardEl.innerHTML = cardInnerHtml;
     // appendChild on an already-attached node relocates it, so both new
     // and reused cards land at the correct sorted position (mirrors
     // _diffOverviewPlanRows' reposition-on-reuse pattern).
     columnBodyEl.appendChild(cardEl);
     newKeys.add(key);
  }
  // Remove cards not in new set.
  for (const key in existing) {
    if (!newKeys.has(key)) {
      existing[key].remove();
    }
  }
}

function chip(dim, value, label, active, color) {
  const style = color ? ` style="--badge-color: var(--c-${color})"` : "";
  return `<button class="filter-chip${active ? " active" : ""}"`
    + ` data-dim="${escapeHtml(dim)}" data-value="${escapeHtml(value)}"${style}>`
    + `${escapeHtml(label)}</button>`;
}

function renderFilterBar(stories) {
  const all = Object.values(stories);
  const personas = [...new Set(all.map((s) => s.persona).filter(Boolean))].sort();
  const risks = [...new Set(all.map((s) => s.risk).filter(Boolean))]
    .sort((a, b) => (RISK_RANK[b] || 0) - (RISK_RANK[a] || 0));
  // Show the Backend / Escalated groups only when at least one story in
  // this plan has a non-local backend or has been escalated. Most plans
  // will be all-local, all-non-escalated — surfacing "yes" / "claude"
  // chips when they would always return zero results just adds noise.
  // When the relevant field is missing on every story we still render
  // the group with the default values; a later poll that introduces
  // escalation will populate it without a code change.
  const hasClaudeBackend = all.some((s) => s && s.backend === "claude");
  const hasAnyEscalation = all.some((s) => s && s.escalated);

  const { filters } = state;
  const term = (filters.search || "").trim().toLowerCase();
  const matchCount = all.filter((s) =>
    term === "" || (String(s.key || "") + " " + (s.summary || "")).toLowerCase().includes(term)
  ).length;
  const countLabel = term === "" ? "" : `<span class="filter-match-count">${matchCount} match${matchCount === 1 ? "" : "es"}</span>`;
  const groups = [
    `<div class="filter-group filter-search-group"><span class="filter-group-label">Search</span>` +
    `<input class="filter-search" type="search" placeholder="Search key or summary…" value="${escapeHtml(filters.search || "")}" data-action="search"></input>` +
    `${countLabel}</div>`,
    `<div class="filter-group"><span class="filter-group-label">Status</span>${
      STATUS_COLUMNS.map((s) =>
        chip("statuses", s, s, filters.statuses.includes(s), s)).join("")
    }</div>`,
  ];
  if (personas.length) {
    groups.push(`<div class="filter-group"><span class="filter-group-label">Persona</span>${
      personas.map((p) => chip("personas", p, p, filters.personas.includes(p))).join("")
    }</div>`);
  }
  if (risks.length) {
    groups.push(`<div class="filter-group"><span class="filter-group-label">Risk</span>${
      risks.map((r) => chip("risks", r, r, filters.risks.includes(r))).join("")
    }</div>`);
  }
  // Backend group: only render the row when a claude backend actually
  // exists in the plan. We always offer both "local" and "claude" so the
  // user can pin to either side; "local" covers the implicit-default
  // case (no backend field present) plus explicitly-local stories.
  if (hasClaudeBackend) {
    groups.push(`<div class="filter-group"><span class="filter-group-label">Backend</span>${
      BACKEND_VALUES.map((b) =>
        chip("backends", b, b, filters.backends.includes(b), b === "claude" ? "accent" : null)
      ).join("")
    }</div>`);
  }
  if (hasAnyEscalation) {
    groups.push(`<div class="filter-group"><span class="filter-group-label">Escalated</span>${
      ESCALATED_VALUES.map((e) =>
        chip("escalated", e, e, filters.escalated.includes(e), e === "yes" ? "parked" : null)
      ).join("")
    }</div>`);
  }
  groups.push(`<div class="filter-group"><span class="filter-group-label">Sort</span>${
    SORT_OPTIONS.map(([v, label]) => chip("sort", v, label, filters.sort === v)).join("")
  }</div>`);

  return `<div class="filter-bar">${groups.join("")}`
    + `<button class="filter-reset" data-action="reset">Reset</button></div>`;
}

export {
  STALE_IN_PROGRESS_MINUTES, _diffBoardCards, ageLabelFor, applyFilters, chip,
  escapeHtml, isStaleInProgress, isWedged, relativeAgeLabel, renderBoard,
  renderFilterBar,
};
