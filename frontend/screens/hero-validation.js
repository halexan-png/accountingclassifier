// hero-validation.js — the Hero overlay + Main (workbook drop + validation)
// screen, v2 UI handoff §4.1/§4.2. Lifted composition/timing from
// tempUI/Odyssey.dc.html's hero + first stage (§3.1).

import { ICON_EXCEL, ICON_SPINNER, ICON_CHECK_GREEN, ICON_CLOSE_SM } from './icons.js';
import { escapeHtml } from './escape.js';

export function renderHero(state) {
  const gone = state.heroEntered;
  return `
    <div class="hero-overlay ${gone ? 'hero-overlay--gone' : ''}" data-action="enterMain">
      <div class="hero-content">
        <img src="gnl-logo-front.png" alt="Global Net Lease" class="hero-logo">
        <h1 class="hero-title">A General and Administrative Expense Solution</h1>
      </div>
      <div class="hero-scroll-hint">
        <div class="hero-scroll-label">Scroll</div>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M6 9 L12 15 L18 9" stroke="#A5A299" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </div>
    </div>`;
}

function checklistRow(check) {
  const mark = check.ok === null ? ICON_SPINNER : check.ok ? ICON_CHECK_GREEN : ICON_CLOSE_SM;
  return `
    <div class="validation-row">
      <span class="validation-row-label">${check.label}</span>
      <span style="display:flex;align-items:center">${mark}</span>
    </div>`;
}

// One upload slot (Q2 takes two: the multi-tab G&A workbook + the flat A&T
// workbook). `slot` is 'ga' or 'at'; the shared drag/drop/browse actions read
// it off data-slot. When both slots are filled, app.js auto-flattens the pair.
const SLOTS = {
  ga: { title: 'G&A workbook', sub: 'multi-tab, one MRI account per tab' },
  at: { title: 'A&T workbook', sub: 'flat acquisition & transaction rows' },
};

function slotBox(state, slot) {
  const meta = SLOTS[slot];
  const otherSlot = slot === 'ga' ? 'at' : 'ga';
  const file = slot === 'ga' ? state.gaFile : state.atFile;
  const otherFile = slot === 'ga' ? state.atFile : state.gaFile;
  const dragActive = slot === 'ga' ? state.dragActiveGa : state.dragActiveAt;
  const dragClass = dragActive ? ' dropzone--drag' : '';
  const locked = state.workbookPhase === 'checking';

  const inner = file ? `
      <button data-action="removeQ2" data-slot="${slot}" aria-label="Remove ${meta.title}" class="dropzone-remove-btn">${ICON_CLOSE_SM}</button>
      <div class="dropzone-confirmed">
        <svg width="46" height="46" viewBox="0 0 48 48" fill="none"><rect x="4" y="4" width="40" height="40" rx="9" fill="#107C41"/><path d="M18 17 L30 31 M30 17 L18 31" stroke="#ffffff" stroke-width="3.2" stroke-linecap="round"/></svg>
        <div class="dropzone-filename">${escapeHtml(file.name)}</div>
      </div>` : `
      <div class="dropzone-icon-wrap">
        ${ICON_EXCEL}
        <div class="dropzone-copy">
          <div class="dropzone-title">${meta.title}</div>
          <div class="dropzone-sub">${meta.sub}</div>
        </div>
      </div>`;

  // Q2 only flattens once BOTH slots hold a file (app.js's maybeFlattenQ2) --
  // with only one filled, workbookPhase sits at 'empty' and nothing else on
  // screen changes. Without this, a single dropped file reads as "drag and
  // drop is broken" rather than "waiting on the other workbook".
  const waitingHint = file && !otherFile
    ? `<div class="dropzone-waiting-hint">Waiting on the ${SLOTS[otherSlot].title}</div>` : '';

  return `
    <div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:8px">
      <div class="dropzone dropzone--compact${dragClass}"
           style="width:100%${locked ? ';opacity:.6;pointer-events:none' : ''}"
           data-action="onQ2Browse" data-slot="${slot}"
           data-ondrop="onQ2Drop" data-ondragover="onQ2DragOver"
           data-ondragenter="onQ2DragEnter" data-ondragleave="onQ2DragLeave">
        <input id="q2-input-${slot}" type="file" accept=".xlsb,.xlsx,.xls" data-onchange="onQ2File" data-slot="${slot}" style="display:none">
        ${inner}
      </div>
      ${waitingHint}
    </div>`;
}

export function render(state) {
  const phase = state.workbookPhase;
  const isChecking = phase === 'checking';
  const isConfirmed = phase === 'confirmed';
  const isError = phase === 'error';
  const showResult = phase !== 'empty';

  const f = state.flattenSummary;
  const flattenLine = isConfirmed && f ? `
    <div class="validation-resume-note" style="color:var(--ink-faint)">
      ${f.ga_tabs_included} G&amp;A tab(s) · ${Number(f.at_rows).toLocaleString()} A&amp;T row(s) · ${Number(f.total_rows).toLocaleString()} rows flattened
    </div>` : '';

  const resultHtml = showResult ? `
    <div class="validation-card">
      <div class="validation-title">Validation</div>
      <div class="validation-divider"></div>
      <div>
        ${isChecking
          ? ['File format', 'Worksheet structure', 'Expense records'].map((label) => checklistRow({ label, ok: null })).join('')
          : (state.workbookChecks.length
              ? state.workbookChecks.map((c) => checklistRow(c)).join('')
              : ['File format', 'Worksheet structure', 'Expense records'].map((label) => checklistRow({ label, ok: false })).join(''))}
      </div>
      <div class="validation-status ${isConfirmed ? 'validation-status--ok' : isError ? 'validation-status--error' : 'validation-status--pending'}">
        <span style="display:flex;align-items:center">${isConfirmed ? ICON_CHECK_GREEN : isError ? '' : ICON_SPINNER}</span>
        <span class="validation-status-text">${
          isConfirmed ? `Workbook validated — ${state.workbookRowCount.toLocaleString()} row(s)` :
          isError ? 'Unable to flatten this pair — check both files and their tabs' : 'Flattening the two workbooks…'
        }</span>
      </div>
      ${flattenLine}
      ${isConfirmed ? `<button data-action="continueToLaunch" class="btn btn--dark btn--full" style="margin-top:14px">Continue</button>` : ''}
    </div>` : '';

  return `
    <div class="main-view">
      <h1 class="stage-title">General and Administrative Excel</h1>
      <div class="stage-row" style="align-items:flex-start">
        <div style="display:flex;flex-direction:column;gap:14px;flex:1;min-width:0;max-width:560px">
          <div style="display:flex;gap:14px">
            ${slotBox(state, 'ga')}
            ${slotBox(state, 'at')}
          </div>
          ${showResult ? '' : '<div class="dropzone-sub" style="text-align:center">Drop both workbooks — they’re flattened into one before anything runs.</div>'}
        </div>
        ${resultHtml}
      </div>
    </div>`;
}
