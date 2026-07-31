// mock-adapter.js — scripted fake backend for Wave 1B (mock-first).
//
// Implements EXACTLY the adapter interface app.js calls, matching
// gna_server's real REST/SSE contract (v2 UI handoff §7 — already built in
// gna_server/, see routes_run.py / routes_upload.py / routes_readonly.py /
// routes_settings.py for the authoritative shapes this mirrors):
//
//   getState() -> {workbook, api_key_present, invoice_library, run, model_default, ui_version}
//   uploadWorkbookQ2(gaFile, atFile) -> {name, ga_name, at_name, checks, row_count, has_existing_classifications, flatten}
//   getQuarters() -> {quarters: [{label, rows, ma_rows}], warnings}
//   uploadInvoices(files) -> {saved: [name], rejected: [{name, reason}]}
//   extractContext(file) -> {name, text}
//   getSettings(key) -> {content, path}
//   putSettings(key, content) -> {content, path}
//   startRun({kind, quarter, deal_profile_quarters, min_usd, user_deal_context}) -> {run_id}
//   streamRunEvents(afterSeq, onEvent) -> {close()}
//   confirmRun(confirm_id, answer) -> {ok}
//   cancelRun() -> {ok}
//   shutdownApp() -> {ok}
//   getResults() -> {summary, artifacts}
//   downloadUrl(key) -> string (a Blob URL standing in for the real file)
//   getDoc(key) -> {markdown}
//
// Every number here is fabricated (loosely shaped after a real quarter's
// order of magnitude so the screens look credible), never a real workbook's
// data. No network access at all — this file is the whole "backend" for
// this wave, exactly as the plan requires (v2 handoff §10, Wave 1B).

function delay(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

const MOCK_QUARTERS = [
  { label: '2026Q1', rows: 2323, ma_rows: 97 },
  { label: '2026Q2', rows: 1320, ma_rows: 103 },
];

const MOCK_SETTINGS = {
  classifier: '# Classifier doctrine (mock)\n\nRules the classifier applies when deciding recurring vs. non-recurring...\n',
  dealbuilder: '# Deal builder doctrine (mock)\n\nRules the Phase-1 sweep applies when building the deal profile...\n',
};

const MOCK_DOCS = {
  getting_started: '# Getting Started\n\nMock content for offline UI dev — real copy lives in memo/getting_started.txt.\n',
  odyssey: '# Odyssey\n\nMock content for offline UI dev — real copy lives in memo/odyssey.txt.\n',
  pipeline_overview: '# Pipeline Overview\n\nMock content for offline UI dev.\n',
  invoice_rules: '# Invoice Matching\n\nMock content for offline UI dev.\n',
  context_tiering: '# Context Tiering & Evidence\n\nMock content for offline UI dev.\n',
  input_format: '# Input File Format\n\nMock content for offline UI dev.\n',
  risk_notes: '# Risk & Reference Notes\n\nMock content for offline UI dev.\n',
};

let settingsStore = { ...MOCK_SETTINGS };
let serverState = {
  workbook: null,
  api_key_present: true,
  invoice_library: { dir_ready: true, csv_ready: true },
  run: null,
  model_default: 'claude-sonnet-5',
  ui_version: '0.1.0 (mock)',
};

// ---------------------------------------------------------------------
// Scripted run engine — simulates the real run-manager's SSE timeline
// (gna_server/run_manager.py) closely enough to demo every process/output
// screen state without a real backend.
// ---------------------------------------------------------------------

class MockRunEngine {
  constructor() {
    this.seq = 0;
    this.listeners = new Set();
    this.confirmWaiters = new Map();
    this.confirmCounter = 0;
    this.cancelled = false;
  }

  emit(type, payload) {
    this.seq += 1;
    const entry = { seq: this.seq, ts: new Date().toTimeString().slice(0, 8), type, payload };
    for (const cb of this.listeners) cb(entry);
    return entry;
  }

  subscribe(cb) {
    this.listeners.add(cb);
    return { close: () => this.listeners.delete(cb) };
  }

  async askConfirm(prompt) {
    this.confirmCounter += 1;
    const confirm_id = `c${this.confirmCounter}`;
    this.emit('confirm_request', { confirm_id, prompt });
    return new Promise((resolve) => this.confirmWaiters.set(confirm_id, resolve));
  }

  answerConfirm(confirm_id, answer) {
    const waiter = this.confirmWaiters.get(confirm_id);
    if (!waiter) return false;
    this.confirmWaiters.delete(confirm_id);
    waiter(Boolean(answer));
    return true;
  }

  cancel() {
    this.cancelled = true;
    for (const [id, waiter] of this.confirmWaiters) { waiter(false); this.confirmWaiters.delete(id); }
    this.emit('info', { msg: 'Stop requested -- in-flight batches will finish, then the run stops.' });
    return true;
  }

  async runFullScript(opts) {
    this.emit('section', { title: 'Ingest' });
    await delay(250);
    this.emit('kv', { pairs: [['rows ingested', '11814']] });
    this.emit('section', { title: 'Phase 0: prep + invoices' });
    await delay(300);
    const phase0 = { reclass_fired: 3, closegl_fired: 1, negatives_skipped: 2, resumed: 0, had_invoice_yes: 180, invoice_accessed_yes: 150, invoice_unavailable: 30, invoice_read_failed: 24, errors: 0 };
    this.emit('data', { kind: 'phase0_stats', payload: phase0 });

    this.emit('section', { title: 'Forecast' });
    const sweepForecast = { rows: 97, est_batches: 4, cost_low_usd: 1.2, cost_high_usd: 2.1, max_workers: 4, wall_clock_est_min: 1.5 };
    const classifyForecast = { rows: 1411, rows_with_invoice: 150, est_batches: 18, cost_low_usd: 6.4, cost_high_usd: 11.2, max_workers: 6, wall_clock_est_min: 4.5 };
    this.emit('data', { kind: 'sweep_forecast', payload: sweepForecast });
    this.emit('data', { kind: 'classify_forecast', payload: classifyForecast });

    const proceed = await this.askConfirm('Proceed? [y/N] ');
    if (this.cancelled) { this.emit('run_state', { state: 'interrupted', exit_code: 130 }); return; }
    if (!proceed) { this.emit('run_state', { state: 'declined', exit_code: 0 }); return; }

    // Phase 1 — build deal profile
    this.emit('section', { title: 'Phase 1: deal sweep' });
    this.emit('data', { kind: 'phase', payload: { phase: 'build_profile' } });
    const dealTotal = 97, dealInvTotal = 62;
    for (let done = 0; done <= dealTotal && !this.cancelled; done += Math.ceil(dealTotal / 8)) {
      const d = Math.min(done, dealTotal);
      this.emit('status', { text: `${d}/${dealTotal} rows`, snapshot: { done_rows: d, total_rows: dealTotal, done_batches: Math.round((d / dealTotal) * 4), total_batches: 4, in_flight: d < dealTotal ? 1 : 0, cost_usd: (d / dealTotal) * 1.8, eta_s: dealTotal === d ? 0 : 20, unit: 'rows' } });
      if (d > 0) this.emit('row', { row_idx: 1000 + d, acctnum: 'MR58200000', amount: 4200 + d, currency: 'USD', classification: 'non_recurring', basis: 'ma_account_rule', recognized_deal: `Deal ${d}`, flags: [], phase: 'sweep' });
      await delay(220);
    }
    if (this.cancelled) { this.emit('run_state', { state: 'interrupted', exit_code: 130 }); return; }

    // Classifier ready prompt (mirrors the design prototype's procPrompt state)
    this.emit('data', { kind: 'phase', payload: { phase: 'classify' } });
    await delay(150);

    const clsTotal = classifyForecast.rows;
    for (let done = 0; done <= clsTotal && !this.cancelled; done += Math.ceil(clsTotal / 14)) {
      const d = Math.min(done, clsTotal);
      const batches = Math.round((d / clsTotal) * classifyForecast.est_batches);
      this.emit('status', { text: `${d}/${clsTotal} rows`, snapshot: { done_rows: d, total_rows: clsTotal, done_batches: batches, total_batches: classifyForecast.est_batches, in_flight: d < clsTotal ? 3 : 0, cost_usd: (d / clsTotal) * 9.1, eta_s: clsTotal === d ? 0 : 90, unit: 'rows' } });
      if (d > 0) {
        const cls = ['recurring', 'recurring', 'non_recurring', 'human_review'][Math.floor(Math.random() * 4)];
        this.emit('row', { row_idx: 2000 + d, acctnum: 'GA1000000' + (d % 9), amount: 500 + d, currency: 'USD', classification: cls, basis: cls === 'recurring' ? 'known_vendor' : 'none', recognized_deal: 'none', flags: [], phase: 'classify' });
      }
      await delay(180);
    }
    if (this.cancelled) { this.emit('run_state', { state: 'interrupted', exit_code: 130 }); return; }

    this.emit('data', { kind: 'phase', payload: { phase: 'done' } });
    this.emit('data', { kind: 'closing_tally', payload: {} });
    this.emit('data', { kind: 'outputs', payload: { results: 'results.jsonl', deal_results: 'deal_results.jsonl', summary: 'summary.json', excel: 'classified.xlsx', excel_ok: true } });
    this.emit('run_state', { state: 'done', exit_code: 0 });
  }

  async runDealProfileScript(opts) {
    this.emit('section', { title: 'Phase 1: deal sweep' });
    this.emit('data', { kind: 'phase', payload: { phase: 'build_profile' } });
    const total = 97;
    for (let done = 0; done <= total && !this.cancelled; done += Math.ceil(total / 10)) {
      const d = Math.min(done, total);
      this.emit('status', { text: `${d}/${total} rows`, snapshot: { done_rows: d, total_rows: total, done_batches: Math.round((d / total) * 4), total_batches: 4, in_flight: d < total ? 1 : 0, cost_usd: (d / total) * 1.8, eta_s: total === d ? 0 : 15, unit: 'rows' } });
      await delay(160);
    }
    if (this.cancelled) { this.emit('run_state', { state: 'interrupted', exit_code: 130 }); return; }
    this.emit('run_state', { state: 'done', exit_code: 0 });
  }

  async runRecoverScript() {
    this.emit('section', { title: 'Summary' });
    await delay(300);
    this.emit('data', { kind: 'outputs', payload: { excel: 'classified.xlsx', excel_ok: true } });
    this.emit('run_state', { state: 'done', exit_code: 0 });
  }

  start(kind, opts) {
    this.cancelled = false;
    this.emit('run_state', { state: 'running' });
    const script = kind === 'recover' ? this.runRecoverScript() : kind === 'deal-profile' ? this.runDealProfileScript(opts) : this.runFullScript(opts);
    script.catch((err) => this.emit('run_state', { state: 'error', exit_code: 1, message: String(err) }));
  }
}

let currentRun = null;

// Microsoft Graph (OneDrive/SharePoint invoice links) — mock: "connecting"
// resolves to "connected" after a short scripted delay, no real browser/auth.
let graphStatus = { status: 'idle', error: null };

// ---------------------------------------------------------------------
// Adapter surface
// ---------------------------------------------------------------------

export const adapter = {
  async getState() {
    await delay(80);
    return { ...serverState, run: currentRun ? { active: true, run_id: currentRun.id, kind: currentRun.kind } : null };
  },

  async uploadWorkbookQ2(gaFile, atFile) {
    await delay(500);
    serverState.workbook = { name: 'q2_flat.xlsx', mtime: Date.now() / 1000 };
    return {
      name: 'q2_flat.xlsx',
      ga_name: gaFile.name,
      at_name: atFile.name,
      checks: [
        { label: 'File format', ok: true },
        { label: 'Worksheet structure', ok: true },
        { label: 'Expense records', ok: true },
      ],
      row_count: 3933,
      has_existing_classifications: false,
      flatten: { ga_tabs_included: 44, ga_tabs_skipped: 4, at_rows: 214, total_rows: 3933, header_warnings: [] },
    };
  },

  async getQuarters() {
    await delay(200);
    return { quarters: MOCK_QUARTERS, warnings: [] };
  },

  async uploadInvoices(files) {
    await delay(300);
    const saved = [], rejected = [];
    for (const f of files) {
      if (!/\.pdf$/i.test(f.name)) rejected.push({ name: f.name, reason: 'not a .pdf file' });
      else saved.push(f.name);
    }
    return { saved, rejected };
  },

  async extractContext(file) {
    await delay(120);
    // The mock can't unzip a real .docx; return a placeholder so the flow is
    // visible. For .txt/.md it reads the Blob text for realism.
    if (/\.docx$/i.test(file.name)) return { name: file.name, text: `[extracted text from ${file.name}]` };
    let text = '';
    try { text = await file.text(); } catch (_e) { text = ''; }
    return { name: file.name, text };
  },

  async connectOneDrive() {
    await delay(100);
    graphStatus = { status: 'connecting', error: null };
    setTimeout(() => { graphStatus = { status: 'connected', error: null }; }, 1800);
    return { ...graphStatus };
  },

  async getGraphStatus() {
    await delay(80);
    return { ...graphStatus };
  },

  async getSettings(key) {
    await delay(120);
    return { content: settingsStore[key] || '', path: `doctrines/${key}.md` };
  },

  async putSettings(key, content) {
    await delay(150);
    settingsStore[key] = content;
    return { content, path: `doctrines/${key}.md` };
  },

  async startRun(opts) {
    await delay(150);
    const id = `run-${Date.now()}`;
    currentRun = new MockRunEngine();
    currentRun.id = id;
    currentRun.kind = opts.kind;
    currentRun.start(opts.kind, opts);
    return { run_id: id };
  },

  streamRunEvents(_afterSeq, onEvent) {
    if (!currentRun) return { close() {} };
    return currentRun.subscribe(onEvent);
  },

  async confirmRun(confirm_id, answer) {
    await delay(50);
    return { ok: currentRun ? currentRun.answerConfirm(confirm_id, answer) : false };
  },

  async cancelRun() {
    await delay(50);
    return { ok: currentRun ? currentRun.cancel() : false };
  },

  async shutdownApp() {
    await delay(80);
    // No real server to stop in mock mode -- just acknowledge so the offline UI
    // can still demo the close-confirm and end screen.
    return { ok: true };
  },

  async getResults() {
    await delay(200);
    return {
      summary: {
        tally: {
          by_classification: { recurring: 980, non_recurring: 310, human_review: 84, reclass: 3 },
          by_classification_excl_deal_profile: { recurring: 980, non_recurring: 225, human_review: 84, reclass: 3 },
        },
        usage: { cost_actual_usd: 9.42 },
      },
      artifacts: [
        { key: 'classified', name: 'classified.xlsx', bytes: 482000, mtime: Date.now() / 1000 },
      ],
    };
  },

  downloadUrl(key) {
    const content = `mock ${key} placeholder -- the real adapter downloads the actual file`;
    const blob = new Blob([content], { type: 'text/plain' });
    return URL.createObjectURL(blob);
  },

  async getDoc(key) {
    await delay(100);
    return { markdown: MOCK_DOCS[key] || '# Not found' };
  },
};
