// static/app/render/story-diff.js
//
// Pure, DOM-free renderer for a story's worktree diff (summary + per-file
// diff). String-template like static/app/patch.js. No fetch, no timers, no
// polling: callers pass in already-fetched JSON. Every path and diff line is
// HTML-escaped; the diff body reuses patch.js's renderDiff.

import { renderDiff } from "../patch.js";

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const EMPTY_MESSAGES = {
  no_worktree: "No worktree has been created for this story yet.",
  worktree_gone: "The story's worktree no longer exists.",
  not_a_worktree: "The story's worktree path is not a git worktree.",
  no_base_ref: "No base branch could be resolved to diff against.",
  git_failed: "Git failed while collecting the diff.",
};

// The reason is used in a class name, so only known reasons reach the
// attribute; anything else gets the generic state.
function renderEmptyState(reason) {
  if (Object.hasOwn(EMPTY_MESSAGES, reason)) {
    return `<div class="story-diff-empty story-diff-empty-${reason}">${EMPTY_MESSAGES[reason]}</div>`;
  }
  return '<div class="story-diff-empty">No diff is available for this story.</div>';
}

function renderFileRow(file) {
  const status = escapeHtml(file.status == null ? "" : file.status);
  const oldPath = file.old_path ? ` <span class="story-diff-old-path">(from ${escapeHtml(file.old_path)})</span>` : "";
  const untracked = file.untracked ? ' <span class="story-diff-untracked">untracked</span>' : "";
  const binary = file.binary ? ' <span class="story-diff-binary">binary</span>' : "";
  return [
    '<li class="story-diff-file">',
    `<span class="story-diff-status story-diff-status-${status.replace(/[^A-Za-z?]/g, "")}">${status}</span> `,
    `<span class="story-diff-path">${escapeHtml(file.path)}</span>${oldPath}${untracked}${binary} `,
    `<span class="story-diff-add">+${escapeHtml(file.added || 0)}</span> `,
    `<span class="story-diff-del">-${escapeHtml(file.removed || 0)}</span>`,
    "</li>",
  ].join("");
}

function renderStoryDiffSummary(summary) {
  const s = summary || {};
  if (!s.available) return renderEmptyState(s.reason);
  const totals = s.totals || {};
  const files = Array.isArray(s.files) ? s.files : [];
  const truncated = s.truncated
    ? '<div class="story-diff-truncated">File list truncated: not all changed files are shown.</div>'
    : "";
  return [
    '<div class="story-diff-summary">',
    '<div class="story-diff-totals">',
    `${escapeHtml(totals.files || 0)} files `,
    `<span class="story-diff-add">+${escapeHtml(totals.added || 0)}</span> `,
    `<span class="story-diff-del">-${escapeHtml(totals.removed || 0)}</span>`,
    "</div>",
    truncated,
    `<ul class="story-diff-files">${files.map(renderFileRow).join("")}</ul>`,
    "</div>",
  ].join("");
}

function renderStoryDiffFile(file) {
  const f = file || {};
  if (!f.available) return renderEmptyState(f.reason);
  const header = `<div class="story-diff-file-header">${escapeHtml(f.path == null ? "" : f.path)}</div>`;
  const binary = f.binary ? '<div class="story-diff-binary">Binary file: no text diff.</div>' : "";
  const note = f.truncated
    ? `<div class="story-diff-truncated">${escapeHtml(f.note || "Diff truncated.")}</div>`
    : "";
  return `<div class="story-diff-file-diff">${header}${binary}${note}${renderDiff(f.diff)}</div>`;
}

export { renderStoryDiffSummary, renderStoryDiffFile };
