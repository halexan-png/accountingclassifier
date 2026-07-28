// output.js — Output screen (v2 UI handoff §4.8): the one downloadable
// artifact, classified.xlsx (main ledger + Human Review Report + Run
// Summary + Deal Profile tabs), plus a one-line tally from summary.json and
// a $0 recover offer if the Excel write failed. summary.json and
// quarter_deal_profile.json are read internally for the tally/sheets but
// are never offered as separate downloads -- their content already lives
// inside classified.xlsx.
// Reads GET /api/results rather than an SSE event — cli.cmd_recover has no
// data("outputs", ...)-equivalent event (see handoff/2026-07-17_ui_build/
// HANDOFF_2_server.md's Deviation section), so this is the one reliable
// source of truth for a recover-triggered run too.

import { ICON_EXCEL_SM, ICON_DONE_BIG } from './icons.js';
import { escapeHtml } from './escape.js';

function fmtBytes(n) {
  if (n > 1_000_000) return `${(n / 1_000_000).toFixed(1)} MB`;
  if (n > 1_000) return `${(n / 1_000).toFixed(0)} KB`;
  return `${n} B`;
}

const ARTIFACT_LABELS = { classified: 'classified.xlsx' };

export function render(state) {
  const summary = state.summary;
  // by_classification_excl_deal_profile drops the A&T/M&A account rows that
  // were only read to build the deal profile (auto-classified non_recurring
  // in Phase 1, never sent to Phase-2 classification) — same exclusion the
  // Human Review Report tab already applies, so this tally's non_recurring
  // count matches what actually went through the run.
  const tally = summary?.tally?.by_classification_excl_deal_profile || summary?.tally?.by_classification || {};
  const artifacts = state.artifacts || [];
  const excelArtifact = artifacts.find((a) => a.key === 'classified');
  const excelFailed = !excelArtifact;

  const tallyHtml = Object.keys(tally).length ? `
    <div class="tally-row">
      ${Object.entries(tally).map(([cls, count]) => `
        <div class="stat"><span class="stat-label">${cls.replace('_', ' ')}</span><span class="stat-value">${count}</span></div>
      `).join('')}
      ${summary.usage ? `<div class="stat"><span class="stat-label">Cost</span><span class="stat-value">$${summary.usage.cost_actual_usd.toFixed(2)}</span></div>` : ''}
    </div>` : '';

  return `
    <div class="main-view">
      <div style="display:flex;flex-direction:column;align-items:center;gap:18px">${ICON_DONE_BIG}</div>
      <h1 class="stage-title">Classification complete</h1>

      <div class="output-card">
        ${tallyHtml}

        ${excelFailed ? `
          <div class="banner banner--error">The Excel write failed — nothing was lost (results are durable). Rebuild it at $0:</div>
          <button data-action="recoverNow" class="btn btn--dark">Recover now (zero API cost)</button>
        ` : `
          <div class="download-row">
            ${artifacts.map((a) => `
              <div class="file-chip">
                ${ICON_EXCEL_SM}
                <div>
                  <div class="file-chip-name">${ARTIFACT_LABELS[a.key] || escapeHtml(a.name)}</div>
                  <div class="file-chip-meta">${fmtBytes(a.bytes)}</div>
                </div>
                <button data-action="downloadArtifact" data-key="${a.key}" class="btn btn--outline" style="padding:8px 16px">Download</button>
              </div>
            `).join('')}
          </div>
        `}

        <button data-action="backToLaunchFromOutput" class="btn btn--plain" style="align-self:center;margin-top:10px">Back to launch</button>
      </div>
    </div>`;
}
