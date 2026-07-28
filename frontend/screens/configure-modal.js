// configure-modal.js — Configure modal: the quarter is picked from the REAL
// quarters present in the (flattened) workbook, and Minimum USD is the
// materiality knob (rows below it are skipped). The multi-quarter deal-profile
// build was removed for the simplified Q2 flow — the single run builds the
// picked quarter's deal profile internally.

import { ICON_CLOSE } from './icons.js';

export function render(state) {
  const quarters = state.quarters || [];
  const selected = state.selectedQuarter || (quarters.length ? quarters[quarters.length - 1].label : null);

  const quarterOptions = quarters.map((q) => `
    <option value="${q.label}" ${q.label === selected ? 'selected' : ''}>
      ${q.label} — ${q.rows} rows, ${q.ma_rows} M&amp;A
    </option>`).join('');

  return `
    <div class="modal-overlay" data-action="closeConfig">
      <div class="modal-panel" data-action="stopPropagation" role="dialog" aria-modal="true" aria-label="Configure run">
        <div class="modal-header">
          <div class="modal-title">Configure run</div>
          <button data-action="closeConfig" aria-label="Close" class="modal-close-btn">${ICON_CLOSE}</button>
        </div>

        <div class="config-columns">
          <div class="config-col">
            <div>
              <div class="config-col-title">Scope</div>
              <div class="config-col-sub">One quarter drives both the deal sweep and the classification</div>
            </div>

            ${quarters.length ? `
              <div class="config-field">
                <div class="config-field-row"><span class="config-field-label">Quarter</span></div>
                <select class="select-native" data-onchange="onQuarterSelect">${quarterOptions}</select>
              </div>
            ` : `<div class="config-col-sub">Upload both workbooks first to see their quarters.</div>`}

            <div class="config-field">
              <div class="config-field-row"><span class="config-field-label">Minimum USD</span></div>
              <input type="number" class="number-input" placeholder="999 (default)" value="${state.minUsd ?? ''}" data-oninput="onMinUsdInput" min="0" step="25">
              <div class="tile-hint">Rows whose absolute USD amount is below this are skipped as immaterial. Leave blank for the $999 default.</div>
            </div>
          </div>
        </div>

        <button data-action="saveConfig" class="btn btn--dark" style="margin-top:22px;align-self:flex-end">Save configuration</button>
      </div>
    </div>`;
}
