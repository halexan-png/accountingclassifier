// process.js — live run screen (v2 UI handoff §4.7). Every number here
// comes from `status.snapshot` / `data:phase0_stats` / `row` SSE events —
// never parsed from formatted console text. Stop is cooperative: the UI
// says "stopping", never promises an immediate halt.

import { ICON_CHECK_GREEN, ICON_SPINNER, ICON_BACK } from './icons.js';
import { escapeHtml } from './escape.js';

function phaseChip(key, label, currentPhase) {
  const order = ['build_profile', 'classify', 'done'];
  const done = currentPhase && order.indexOf(currentPhase) > order.indexOf(key);
  const active = currentPhase === key;
  const mark = done || currentPhase === 'done' ? ICON_CHECK_GREEN : active ? ICON_SPINNER : `<span class="chip-dash"></span>`;
  return `<div class="chip">${mark}<span class="chip-label">${label}</span></div>`;
}

function formatEta(seconds) {
  if (seconds == null) return '--';
  const minutes = seconds / 60;
  return minutes < 1 ? '<1m' : `~${Math.round(minutes)}m`;
}

export function render(state) {
  const phase = state.phase;
  const snap = state.statusSnapshot;
  const stopping = state.runState === 'interrupted' || (state.runMessage && /stop/i.test(state.runMessage));

  const statsHtml = snap ? `
    <div class="stat-row">
      <div class="stat"><span class="stat-label">${snap.unit === 'rows' ? 'Rows' : snap.unit}</span><span class="stat-value">${snap.done_rows}/${snap.total_rows}</span></div>
      <div class="stat"><span class="stat-label">Batches</span><span class="stat-value">${snap.done_batches}/${snap.total_batches}</span></div>
      <div class="stat"><span class="stat-label">In flight</span><span class="stat-value">${snap.in_flight}</span></div>
      <div class="stat"><span class="stat-label">Cost so far</span><span class="stat-value">$${snap.cost_usd.toFixed(2)}</span></div>
      <div class="stat"><span class="stat-label">ETA</span><span class="stat-value">${formatEta(snap.eta_s)}</span></div>
    </div>
  ` : `<div class="live-line">Waiting for the first batch<span class="blink-cursor"></span></div>`;

  const phase0 = state.phase0Stats;
  const invoiceBanner = phase0 ? `
    <div class="banner banner--info">Invoices: ${phase0.invoice_accessed_yes || 0} accessed, ${phase0.invoice_unavailable || 0} unavailable of ${phase0.had_invoice_yes || 0} rows with an invoice</div>
  ` : '';

  const doneHtml = phase === 'done' ? `
    <div class="done-row">
      ${ICON_CHECK_GREEN}
      <span class="done-title">Classification complete</span>
    </div>
    <button data-action="backToLaunchFromProcess" class="btn btn--plain">Back to launch</button>
  ` : '';

  const rowsHtml = state.liveRows.length ? `
    <div style="display:flex;flex-direction:column;gap:6px;max-height:180px;overflow-y:auto">
      ${state.liveRows.map((r) => `
        <div style="display:flex;justify-content:space-between;font-size:12.5px;color:var(--ink-secondary-alt);border-bottom:1px solid var(--line-3);padding:4px 0">
          <span>row ${r.row_idx} · ${escapeHtml(r.acctnum)}</span>
          <span>${escapeHtml(r.classification)}</span>
        </div>`).join('')}
    </div>
  ` : '';

  return `
    <div class="main-view">
      <div class="page-shell" style="max-width:760px">
        <button data-action="backToLaunchFromProcess" class="back-btn">${ICON_BACK} Back</button>
      </div>
      <div class="panel">
        <div class="step-chips">
          ${phaseChip('build_profile', 'Build profile', phase)}
          ${phaseChip('classify', 'Classify', phase)}
        </div>
        <div class="panel-body">
          ${stopping ? `<div class="stop-banner">Stopping — in-flight batches will finish, then the run stops.</div>` : ''}
          ${invoiceBanner}
          <div class="panel-section-title">${phase === 'classify' ? 'Classifying' : phase === 'done' ? 'Done' : 'Building deal profile'}</div>
          ${statsHtml}
          ${rowsHtml}
          ${doneHtml}
          ${phase !== 'done' && !stopping ? `<button data-action="stopRun" class="btn btn--danger" style="align-self:flex-start">Stop</button>` : ''}
        </div>
      </div>
    </div>`;
}
