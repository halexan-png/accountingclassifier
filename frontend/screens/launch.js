// launch.js — Launch screen: Additional Context tile, External Invoices
// dropzone, Configure + Run. (Deal-profile import + multi-quarter build were
// removed for the simplified Q2 two-file flow — the single run builds the
// quarter's deal profile internally.)

import { ICON_BACK, ICON_PLUS, ICON_UPLOAD, ICON_GEAR, ICON_CHECK_GREEN } from './icons.js';
import { escapeHtml } from './escape.js';

function wordCount(str) { return str.trim() ? str.trim().split(/\s+/).length : 0; }

// OneDrive/SharePoint access is optional -- connecting lets invoice_read
// fetch OneDrive/SharePoint invoice links via Microsoft Graph instead of
// failing them anonymously (see gna_pipeline/graph_auth.py). Not connecting
// is fine; Run then requires the acknowledgment checkbox below instead.
function oneDriveTile(state) {
  const status = state.oneDriveStatus;
  const connected = status === 'connected';
  const connecting = status === 'connecting';
  // Only shows once a blocked Run click set the flag, and only while it's
  // still the actual reason Run is blocked (not once ack/connect resolves).
  const ackMissing = state.oneDriveAckMissing && !connected && !state.oneDriveAck;

  return `
    <div class="tile" style="cursor:default">
      <div class="tile-header">
        <span class="tile-label">OneDrive access</span>
        ${connected ? `<span style="display:flex;align-items:center">${ICON_CHECK_GREEN}</span>` : ''}
      </div>
      ${connected ? `
        <div class="tile-hint">Connected — OneDrive/SharePoint invoice links will be read.</div>
      ` : `
        <button type="button" data-action="connectOneDrive" class="dashed-btn" ${connecting ? 'disabled' : ''}>
          ${ICON_PLUS} ${connecting ? 'Connecting…' : 'Connect OneDrive'}
        </button>
        <div class="tile-hint">Optional — only needed if this workbook's invoice links point to OneDrive/SharePoint. A browser window will open.</div>
        ${status === 'error' ? `<div class="tile-hint" style="color:var(--error-ink)">Sign-in failed${state.oneDriveError ? `: ${escapeHtml(state.oneDriveError)}` : ''} — try again.</div>` : ''}
        <div class="ready-row" style="margin-top:2px;padding:12px${ackMissing ? ';border-color:var(--error-ink);background:var(--error-bg)' : ''}">
          <input type="checkbox" id="onedrive-ack-checkbox" class="ready-checkbox" ${state.oneDriveAck ? 'checked' : ''} data-onchange="toggleOneDriveAck">
          <label for="onedrive-ack-checkbox" class="ready-copy">I understand OneDrive/SharePoint invoice links won't be readable without connecting.</label>
        </div>
        ${ackMissing ? `<div class="tile-hint" style="color:var(--error-ink);font-weight:700">Check this box, or connect OneDrive, before running.</div>` : ''}
      `}
    </div>`;
}

export function render(state) {
  const ctxCount = wordCount(state.userDealContext);
  const gaugeClass = ctxCount > 3500 ? 'word-gauge--over' : ctxCount >= 2950 ? 'word-gauge--warn' : '';
  const invoiceCount = state.invoiceFiles.length;
  const oneDriveOk = state.oneDriveStatus === 'connected' || state.oneDriveAck;
  const runReady = state.configured && oneDriveOk;

  return `
    <div class="main-view">
      <div class="page-shell">
        <button data-action="goHome" class="back-btn">${ICON_BACK} Back</button>
      </div>
      <h1 class="stage-title">Launch</h1>

      <div class="launch-card">
        <div class="launch-tiles">

          <div class="tile" data-action="openCtx" data-ondrop="onCtxDrop" data-ondragover="onDragOver">
            <div class="tile-header">
              <span class="tile-label">Additional context</span>
              <span class="word-gauge ${gaugeClass}">${ctxCount} / 3500 words</span>
            </div>
            <button data-action="openCtx" class="dashed-btn">${ICON_PLUS} ${ctxCount > 0 ? 'Edit context' : 'Add context'}</button>
            ${ctxCount > 0 ? `<div class="tile-preview">${escapeHtml(state.userDealContext)}</div>` : ''}
            <div class="tile-hint">Click to modify, or drop a .txt / .md / .docx file here</div>
          </div>

          <div class="deal-tile" data-action="onInvoiceBrowse" data-ondrop="onInvoiceDrop" data-ondragover="onDragOver">
            <input id="invoice-input" type="file" accept=".pdf" multiple data-onchange="onInvoiceFiles" style="display:none">
            <input id="invoice-folder-input" type="file" webkitdirectory directory multiple data-onchange="onInvoiceFiles" style="display:none">
            ${ICON_UPLOAD}
            <div class="deal-tile-title-wrap">
              <div class="tile-label">External invoices</div>
              <div class="tile-hint">${invoiceCount ? `${invoiceCount} file(s) added` : 'Drop already-named PDFs (or a whole folder), or click to browse'}</div>
              <button type="button" data-action="onInvoiceFolderBrowse" class="btn btn--outline tile-folder-btn">or choose a folder</button>
              ${invoiceCount ? '' : `<div class="tile-hint" style="margin-top:-2px">Tip: if the folder dialog only lets you "Open" (not select a folder), navigate into it, press Ctrl+A to select every file inside, then click Open — that adds them all. Dragging the folder onto this tile works too.</div>`}
            </div>
          </div>

          ${oneDriveTile(state)}
        </div>

        <div class="actions-row">
          <button data-action="openConfig" class="btn btn--outline">${ICON_GEAR} Configure</button>
          <button data-action="runBtnClick" class="btn ${runReady ? 'btn--primary' : ''}" ${runReady ? '' : 'style="background:var(--action-disabled);color:#fff;padding:15px 52px;font-size:15px;letter-spacing:.14em;cursor:pointer"'}>Run</button>
        </div>
      </div>
    </div>`;
}
