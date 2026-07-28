// settings.js — Settings (v2 UI handoff §4.10): the doctrine editors,
// permanent writes straight to the real file (the only disk-write path for
// doctrine content — Additional Context on Launch stays session-only).
// Also folds in the Security panel (§9): key present/absent (never editable
// here — that's an ops/file task outside the app) and loopback-only note.

import { ICON_BACK } from './icons.js';
import { escapeHtml } from './escape.js';

const TABS = [
  { key: 'classifier', label: 'Classifier' },
  { key: 'dealbuilder', label: 'Deal builder' },
  { key: 'companynorm', label: 'Company norms' },
];

export function render(state) {
  return `
    <div class="main-view main-view--top">
      <div class="page-shell page-shell--narrow">
        <button data-action="goBack" class="back-btn" style="margin-bottom:10px">${ICON_BACK} Back</button>
        <h1 class="stage-title" style="text-align:left;font-size:28px">Settings</h1>

        <div class="settings-tabs" style="margin-top:24px">
          ${TABS.map((t) => `<button data-action="selectSettingsTab" data-key="${t.key}" class="settings-tab ${state.settingsKey === t.key ? 'settings-tab--active' : ''}">${t.label}</button>`).join('')}
        </div>
        <textarea class="settings-editor" data-oninput="onSettingsInput">${escapeHtml(state.settingsContent)}</textarea>
        <div class="settings-save-row">
          ${state.settingsSavedNote ? `<span class="settings-saved-note">${state.settingsSavedNote}</span>` : ''}
          <button data-action="saveSettings" class="btn btn--dark">Save (permanent)</button>
        </div>

        <h2 style="font-weight:800;font-size:15px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-primary-alt);margin-top:30px">Security</h2>
        <div class="security-row">
          <span class="security-label">API key</span>
          <span class="badge ${state.apiKeyPresent ? 'badge--ok' : 'badge--warn'}">${state.apiKeyPresent ? 'Present' : 'Missing'}</span>
        </div>
        <div class="security-row">
          <span class="security-label">Local invoice library</span>
          <span class="badge ${state.invoiceLibrary.dir_ready && state.invoiceLibrary.csv_ready ? 'badge--ok' : 'badge--warn'}">${state.invoiceLibrary.dir_ready && state.invoiceLibrary.csv_ready ? 'Ready' : 'Off'}</span>
        </div>
        <div class="security-row" style="border-bottom:none">
          <span class="security-label">Network</span>
          <span class="badge badge--ok">Loopback only (127.0.0.1)</span>
        </div>
        <div class="tile-hint" style="margin-top:8px">The API key lives in .env and is never sent to the browser — it can't be entered or edited here.</div>
      </div>
    </div>`;
}
