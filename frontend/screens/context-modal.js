// context-modal.js — Additional Context modal (v2 UI handoff §4.3).
// Session-only: never written to disk (kwarg-only user_deal_context_override
// on the run request); resets on refresh.

import { ICON_CLOSE, ICON_UPLOAD, ICON_ERROR, ICON_WARNING } from './icons.js';
import { escapeHtml } from './escape.js';

const WORD_MAX = 3500;
function wordCount(str) { return str.trim() ? str.trim().split(/\s+/).length : 0; }

export function render(state) {
  const count = wordCount(state.userDealContext);
  const over = count > WORD_MAX;
  return `
    <div class="modal-overlay" data-action="closeCtx">
      <div class="modal-panel" data-action="stopPropagation" role="dialog" aria-modal="true" aria-label="Additional context">
        <div class="modal-header">
          <div class="modal-title">Additional context</div>
          <div style="display:flex;align-items:center;gap:16px">
            <span id="ctx-word-count" class="word-gauge${over ? ' word-gauge--over' : ''}">${count} / ${WORD_MAX} words</span>
            <button data-action="closeCtx" aria-label="Close" class="modal-close-btn">${ICON_CLOSE}</button>
          </div>
        </div>
        <textarea id="ctx-textarea" class="textarea" placeholder="Type or paste any context that should guide the classification…" data-oninput="onCtxInput">${escapeHtml(state.userDealContext)}</textarea>
        <div class="file-drop-strip" data-action="onCtxBrowse" data-ondrop="onCtxDrop" data-ondragover="onDragOver">
          <input id="ctx-file-input" type="file" accept=".txt,.md,.docx" data-onchange="onCtxFile" style="display:none">
          ${ICON_UPLOAD}
          <span style="font-weight:500;font-size:13px">Drop a .txt, .md, or .docx file, or click to browse</span>
        </div>
        <div id="ctx-dilute-warning" class="warning-note" style="margin-top:10px;display:${over ? 'flex' : 'none'}">
          ${ICON_WARNING}<span class="warning-note-text">Warning: context can dilute other reasoning.</span>
        </div>
        ${state.ctxError ? `
          <div class="error-note">${ICON_ERROR}<span class="error-note-text">${state.ctxError}</span></div>
        ` : ''}
        <button data-action="closeCtx" class="btn btn--dark" style="margin-top:16px;align-self:flex-end">Done</button>
      </div>
    </div>`;
}
