// guide.js — Guide (v2 UI handoff §4.11): subpages rendered from the real
// repo docs (Guide / How It Works), not one combined block.

import { ICON_BACK } from './icons.js';
import { renderMarkdown } from './markdown.js';

const TABS = [
  { key: 'quickstart', label: 'Guide' },
  { key: 'how_it_works', label: 'How It Works' },
];

export function render(state) {
  return `
    <div class="main-view main-view--top">
      <div class="page-shell page-shell--wide">
        <button data-action="goBack" class="back-btn" style="margin-bottom:10px">${ICON_BACK} Back</button>
        <h1 class="stage-title" style="text-align:left;font-size:28px">Guide</h1>
        <div class="guide-tabs" style="margin-top:20px">
          ${TABS.map((t) => `<button data-action="selectGuideTab" data-key="${t.key}" class="settings-tab ${state.guideKey === t.key ? 'settings-tab--active' : ''}">${t.label}</button>`).join('')}
        </div>
        <div class="markdown-view">${renderMarkdown(state.guideMarkdown)}</div>
      </div>
    </div>`;
}
