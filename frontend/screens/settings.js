// settings.js — Settings (v2 UI handoff §4.10): the doctrine editors,
// permanent writes straight to the real file (the only disk-write path for
// doctrine content — Additional Context on Launch stays session-only).
// Also folds in the Security panel (§9): key present/absent (never editable
// here — that's an ops/file task outside the app) and loopback-only note.
//
// Each doctrine shows as a summary tile (name + one-line description, mirroring
// Launch's Additional Context tile) rather than an always-open textarea; the
// tile's "Edit doctrine" button opens a large modal to do the actual editing,
// so the editing surface gets far more room than the page's narrow column
// allows and the permanent-write warning is unmissable right above the text.

import { ICON_BACK, ICON_PLUS, ICON_CLOSE, ICON_WARNING } from './icons.js';
import { escapeHtml } from './escape.js';

const TABS = [
  {
    key: 'classifier', label: 'Classifier',
    description: 'Governs how each G&A row is classified as recurring, non-recurring, or flagged for human review.',
  },
  {
    key: 'dealbuilder', label: 'Deal builder',
    description: "Governs how Acquisition & Transaction rows and their invoices are read to build the quarter's deal vocabulary.",
  },
  {
    key: 'companynorm', label: 'Company norms',
    description: 'Permanent, company-specific context — routine vendors and account conventions — read into every classification prompt.',
  },
];

function activeTab(state) {
  return TABS.find((t) => t.key === state.settingsKey) || TABS[0];
}

function renderEditModal(state, tab) {
  return `
    <div class="modal-overlay" data-action="closeSettingsEdit">
      <div class="modal-panel modal-panel--editor" data-action="stopPropagation" role="dialog" aria-modal="true" aria-label="${tab.label} doctrine">
        <div class="modal-header">
          <div class="modal-title">${tab.label} doctrine</div>
          <button data-action="closeSettingsEdit" aria-label="Close" class="modal-close-btn">${ICON_CLOSE}</button>
        </div>
        <div class="modal-confirm-reminder">${ICON_WARNING}<span>Changes made are permanent and cannot be undone — saving a copy on your personal laptop first is recommended.</span></div>
        <textarea class="settings-editor settings-editor--modal" data-oninput="onSettingsInput">${escapeHtml(state.settingsContent)}</textarea>
        <div class="settings-save-row">
          <button data-action="saveSettings" class="btn btn--dark">Save changes</button>
        </div>
      </div>
    </div>`;
}

export function render(state) {
  const tab = activeTab(state);
  return `
    <div class="main-view main-view--top">
      <div class="page-shell page-shell--narrow">
        <button data-action="goBack" class="back-btn" style="margin-bottom:10px">${ICON_BACK} Back</button>
        <h1 class="stage-title" style="text-align:left;font-size:28px">Settings</h1>

        <div class="settings-tabs" style="margin-top:24px">
          ${TABS.map((t) => `<button data-action="selectSettingsTab" data-key="${t.key}" class="settings-tab ${state.settingsKey === t.key ? 'settings-tab--active' : ''}">${t.label}</button>`).join('')}
        </div>

        <div class="tile" data-action="openSettingsEdit" style="margin-top:20px">
          <div class="tile-header"><span class="tile-label">${tab.label} doctrine</span></div>
          <div class="tile-preview" style="max-height:none">${tab.description}</div>
          <button data-action="openSettingsEdit" class="dashed-btn">${ICON_PLUS} Edit doctrine</button>
        </div>

        <h2 style="font-weight:800;font-size:15px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-primary-alt);margin-top:30px">Security</h2>
        <div class="security-row">
          <span class="security-label">API key</span>
          <span class="badge ${state.apiKeyPresent ? 'badge--ok' : 'badge--warn'}">${state.apiKeyPresent ? 'Present' : 'Missing'}</span>
        </div>
        <div class="security-row" style="border-bottom:none">
          <span class="security-label">Network</span>
          <span class="badge badge--ok">Loopback only (127.0.0.1)</span>
        </div>
        <div class="tile-hint" style="margin-top:8px">The API key and Graph IDs live in .env and are never sent to the browser — they can't be viewed or entered here. You can, however, clear them to force a fresh setup at the next launch.</div>

        <button data-action="openResetCredentials" class="btn btn--danger" style="margin-top:18px">Reset credentials</button>
        <div class="tile-hint" style="margin-top:8px">Permanently wipes the Anthropic API key and the Microsoft Graph tenant/client IDs from this computer, then closes the app. The next time you double-click <strong>Start.cmd</strong>, it asks you to enter them from scratch.</div>
        <div class="tile-hint" style="margin-top:6px">Note: if you enter an API key but skip the Graph IDs, the launcher won't ask for the Graph IDs again on later launches. The only way to be prompted for them again is to reset here — which clears the API key too, so you re-enter everything together.</div>
      </div>
    </div>
    ${state.settingsEditOpen ? renderEditModal(state, tab) : ''}`;
}
