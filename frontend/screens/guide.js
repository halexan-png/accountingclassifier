// guide.js — Guide (v2 UI handoff §4.11, rebuilt): three sections rendered
// from the real memo/ docs. Getting Started and What's Going On are each a
// single document; Specifics is a six-document sub-index of
// memo/plutos_gneseos/, picked via a secondary tab row. Doc content is still
// fetched by key through adapter.getDoc/state.guideMarkdown exactly as
// before — this file only changes which keys exist and how they're grouped.

import { ICON_BACK, ICON_DOWNLOAD } from './icons.js';
import { renderMarkdown } from './markdown.js';

const TOP_TABS = [
  { key: 'getting_started', label: 'Getting Started' },
  { key: 'odyssey', label: "What's Going On" },
  { key: 'additional_context', label: 'Additional Context' },
];

// The six memo/plutos_gneseos/ documents, in reading order. The "Specifics"
// top-level tab is this same list's first entry — clicking it is identical
// to picking the first sub-document, so no separate action/state is needed
// to represent "Specifics" as its own concept.
//
// risk_notes and field_glossary were one document until they were split:
// risk_notes covers narrative, read-once operational behavior (a stopped
// run, a truncated invoice); field_glossary is pure lookup reference (what
// a column or flag means) — different reading modes, so they get separate
// tabs rather than one document serving both.
const SPECIFICS_TABS = [
  { key: 'pipeline_overview', label: 'Pipeline Overview' },
  { key: 'invoice_rules', label: 'Invoice Matching' },
  { key: 'context_tiering', label: 'Context Tiering & Evidence' },
  { key: 'input_format', label: 'Input File Format' },
  { key: 'risk_notes', label: 'Risk & Reference Notes' },
  { key: 'field_glossary', label: 'Field Glossary' },
];

function isSpecificsKey(key) {
  return SPECIFICS_TABS.some((t) => t.key === key);
}

function pill(key, label, active) {
  return `<button data-action="selectGuideTab" data-key="${key}" class="settings-tab ${active ? 'settings-tab--active' : ''}">${label}</button>`;
}

function subtab(key, label, active) {
  return `<button data-action="selectGuideTab" data-key="${key}" class="guide-subtab ${active ? 'guide-subtab--active' : ''}">${label}</button>`;
}

export function render(state) {
  const specificsActive = isSpecificsKey(state.guideKey);
  return `
    <div class="main-view main-view--top">
      <div class="page-shell page-shell--wide">
        <button data-action="goBack" class="back-btn" style="margin-bottom:10px">${ICON_BACK} Back</button>
        <h1 class="stage-title" style="text-align:left;font-size:28px">Guide</h1>

        <div class="guide-tabs" style="margin-top:20px">
          ${TOP_TABS.map((t) => pill(t.key, t.label, state.guideKey === t.key)).join('')}
          ${pill(SPECIFICS_TABS[0].key, 'Specifics', specificsActive)}
        </div>

        ${specificsActive ? `
          <div class="guide-subtabs">
            ${SPECIFICS_TABS.map((t) => subtab(t.key, t.label, state.guideKey === t.key)).join('')}
          </div>
        ` : ''}

        ${state.guideKey === 'getting_started' ? `
          <div class="sample-download-row">
            <button data-action="downloadSample" data-key="sample_ga" class="btn btn--outline">${ICON_DOWNLOAD}<span>Download sample G&amp;A workbook</span></button>
            <button data-action="downloadSample" data-key="sample_at" class="btn btn--outline">${ICON_DOWNLOAD}<span>Download sample A&amp;T workbook</span></button>
          </div>
        ` : ''}

        ${state.guideKey === 'additional_context' ? `
          <div class="sample-download-row">
            <button data-action="downloadSample" data-key="additional_context_skill" class="btn btn--outline">${ICON_DOWNLOAD}<span>Download context skill (SKILL.md)</span></button>
          </div>
        ` : ''}

        <div class="markdown-view">${renderMarkdown(state.guideMarkdown)}</div>
      </div>
    </div>`;
}
