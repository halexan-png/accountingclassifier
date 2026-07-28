// forecast.js — Forecast + money gate (v2 UI handoff §4.6). NEW: not in the
// design prototype, which skipped straight to a running spinner. Every
// number here comes from `data:sweep_forecast`/`data:classify_forecast`
// SSE events, never parsed from formatted text (v2 §7.2's absolute rule).
// The "ready" checkbox gates Proceed; Proceed/Cancel answer the pipeline's
// real console.confirm() gate. No agent ever ticks the box or clicks
// Proceed (v2 §8) — that action exists solely for the human operator.

import { ICON_SPINNER_LG } from './icons.js';

function money(lo, hi) { return `$${lo.toFixed(2)}–$${hi.toFixed(2)}`; }

export function render(state) {
  const sweep = state.sweepForecast;
  const cls = state.classifyForecast;
  const waitingForConfirm = state.runState === 'awaiting_confirm' && state.confirmId;

  if (!cls) {
    return `
      <div class="main-view">
        ${ICON_SPINNER_LG}
        <div class="stage-title" style="text-transform:none;font-size:16px">Preparing the forecast…</div>
      </div>`;
  }

  const spendRail = cls.cost_high_usd * 1.15;

  return `
    <div class="main-view">
      <h1 class="stage-title">Forecast</h1>
      <div class="panel" style="max-width:640px">
        <div class="panel-body forecast-card">
          ${sweep ? `
            <div class="panel-section-title">Deal-profile sweep</div>
            <div class="forecast-grid">
              <div class="forecast-item"><span class="forecast-item-label">Rows</span><span class="forecast-item-value">${sweep.rows}</span></div>
              <div class="forecast-item"><span class="forecast-item-label">Batches</span><span class="forecast-item-value">~${sweep.est_batches}</span></div>
              <div class="forecast-item"><span class="forecast-item-label">Cost</span><span class="forecast-item-value">${money(sweep.cost_low_usd, sweep.cost_high_usd)}</span></div>
            </div>
          ` : ''}

          <div class="panel-section-title">Classification</div>
          <div class="forecast-grid">
            <div class="forecast-item"><span class="forecast-item-label">Rows</span><span class="forecast-item-value">${cls.rows}</span></div>
            <div class="forecast-item"><span class="forecast-item-label">With invoice</span><span class="forecast-item-value">${cls.rows_with_invoice}</span></div>
            <div class="forecast-item"><span class="forecast-item-label">Batches</span><span class="forecast-item-value">~${cls.est_batches}</span></div>
            <div class="forecast-item"><span class="forecast-item-label">Workers</span><span class="forecast-item-value">${cls.max_workers}</span></div>
            <div class="forecast-item"><span class="forecast-item-label">Wall clock</span><span class="forecast-item-value">~${cls.wall_clock_est_min.toFixed(1)} min</span></div>
            <div class="forecast-item"><span class="forecast-item-label">Total estimate</span><span class="forecast-item-value forecast-cost">${money(cls.cost_low_usd, cls.cost_high_usd)}</span></div>
          </div>
          <div class="tile-hint">Spend rail aborts past $${spendRail.toFixed(2)} (1.15× the high estimate) — whatever is already decided stays durable.</div>

          <div class="ready-row" style="margin-top:12px">
            <input type="checkbox" id="ready-checkbox" class="ready-checkbox" ${state.readyChecked ? 'checked' : ''} data-onchange="toggleReady">
            <label for="ready-checkbox" class="ready-copy">I'm ready to start a paid run.</label>
          </div>

          <div class="actions-row" style="margin-top:6px">
            <button data-action="cancelForecast" class="btn btn--outline">Cancel</button>
            <button data-action="proceedRun" class="btn btn--primary" ${state.readyChecked && waitingForConfirm ? '' : 'disabled'}>Proceed</button>
          </div>
          ${!waitingForConfirm ? `<div class="tile-hint">Waiting on the pipeline's confirm gate…</div>` : ''}
        </div>
      </div>
    </div>`;
}
