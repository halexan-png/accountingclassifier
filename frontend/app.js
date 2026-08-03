// app.js — hand-rolled view store + router + event delegation.
//
// No framework, no build step (v2 UI handoff §5): one flat `state` object,
// `setState(patch)` merges + re-renders, screens are pure `render(state) ->
// html string` functions. User interaction never wires an inline onclick —
// every element that does something carries a `data-action="name"` (or
// `data-oninput="name"` / `data-onchange="name"`) attribute, and ONE
// delegated listener per event type (attached once, here) looks the name up
// in `actions` and calls it. This is the same "hand-rolled, no rich
// framework" spirit as gna_pipeline/console.py.
//
// Wave 3 swapped the adapter from `mock-adapter.js` (scripted fake data, no
// network) to `real-adapter.js` (talks to gna_server's actual REST/SSE
// endpoints). This is the ONLY change that swap required — every screen and
// action was written against this shared interface from the start (see
// mock-adapter.js's header comment for the exact contract). `mock-adapter.js`
// stays in the tree as the interface reference and for offline UI dev: point
// this one import back at it to run the whole app with no backend.

import { adapter } from './real-adapter.js';

import * as heroValidation from './screens/hero-validation.js';
import * as launch from './screens/launch.js';
import * as configModal from './screens/configure-modal.js';
import * as contextModal from './screens/context-modal.js';
import * as forecast from './screens/forecast.js';
import * as process_ from './screens/process.js';
import * as output from './screens/output.js';
import * as settings from './screens/settings.js';
import * as guide from './screens/guide.js';
import * as timeout_ from './screens/timeout.js';
import * as closed_ from './screens/closed.js';
import { escapeHtml } from './screens/escape.js';
import { ICON_WARNING } from './screens/icons.js';

const root = document.getElementById('app-root');

// ---------------------------------------------------------------------
// State
// ---------------------------------------------------------------------

const state = {
  view: 'main', // 'main' | 'launch' | 'forecast' | 'process' | 'output' | 'settings' | 'guide'
  prevView: 'main',
  heroEntered: false,

  // Set true once the liveness ping (below) can no longer reach the server,
  // i.e. it idle-timed-out and shut down. Short-circuits render() to the
  // terminal "session timed out" screen.
  sessionTimedOut: false,

  // Deliberate "Close application" (Output screen). sessionClosed short-circuits
  // render() to the terminal "you're all set" end screen once the server has
  // been asked to stop -- distinct from sessionTimedOut so the copy can say "you
  // closed it" rather than "it timed out". closeConfirmOpen drives the "Close
  // the application?" confirm dialog; closing is true only while the shutdown
  // POST is in flight (disables the confirm buttons so it can't be double-fired).
  sessionClosed: false,
  closeConfirmOpen: false,
  closing: false,

  // "Reset credentials" (Settings → Security). Wipes the API key + Graph IDs
  // from .env and stops the server (routes_lifecycle.reset_credentials), so the
  // next launch re-runs first-time setup. resetConfirmOpen drives the confirm
  // dialog; resetAck gates the danger button (the operator must tick "I
  // understand this is permanent"); resetting is true only while the POST is in
  // flight. On success the terminal end screen shows reset-specific copy, keyed
  // off closedReason so it says "credentials cleared" rather than "closed".
  resetConfirmOpen: false,
  resetAck: false,
  resetting: false,
  closedReason: null, // null | 'reset' — which copy the terminal 'closed' screen shows

  // Q2 two-file upload + validation: the multi-tab G&A workbook and the flat
  // A&T workbook are flattened server-side into ONE workbook that everything
  // downstream reads. Phase tracks that flatten+validate step.
  workbookPhase: 'empty', // empty | checking | confirmed | error
  workbookName: '',
  workbookChecks: [],
  workbookRowCount: 0,
  hasExistingClassifications: false,
  gaFile: null, // {name} | null — the G&A slot
  atFile: null, // {name} | null — the A&T slot
  dragActiveGa: false,
  dragActiveAt: false,
  flattenSummary: null, // {ga_tabs_included, ga_tabs_skipped, at_rows, total_rows}

  // quarters (real quarters present in the flattened workbook)
  quarters: [],
  quartersWarnings: [],

  // Launch inputs: Additional Context + External Invoices
  userDealContext: '',
  invoiceFiles: [], // [{name, ok, reason?}]
  ctxOpen: false,
  ctxError: '',
  ctxOverCap: false,

  // OneDrive/SharePoint access (optional) — connecting lets invoice_read
  // fetch OneDrive/SharePoint invoice links via Microsoft Graph instead of
  // failing them anonymously. Not connecting is fine; Run then requires
  // oneDriveAck instead (see actions.runBtnClick).
  oneDriveStatus: 'idle', // idle | connecting | connected | error
  oneDriveError: null,
  oneDriveAck: false,
  oneDriveAckMissing: false, // true after a blocked Run attempt -- highlights the ack checkbox red

  // Configure modal: quarter pick + min-USD materiality knob
  configOpen: false,
  configured: false,
  selectedQuarter: null,
  minUsd: null,

  // Forecast + money gate (v2 §4.6 — new, not in the design prototype)
  runKind: null, // 'run' | 'deal-profile' | 'recover'
  runId: null,
  runState: 'idle',
  reattaching: false, // true only while rebuilding a live view after a page refresh mid-run
  sweepForecast: null,
  classifyForecast: null,
  readyChecked: false,
  confirmId: null,
  confirmPrompt: '',

  // Process / live run (v2 §4.7)
  phase: null, // 'build_profile' | 'classify' | 'done'
  statusSnapshot: null,
  phase0Stats: null,
  liveRows: [],
  tally: {},
  runMessage: '',

  // Output (v2 §4.8)
  summary: null,
  artifacts: [],
  // Auto-download + close gate. On a successful run the workbook downloads
  // itself once (autoDownloaded latches that so a re-render can't re-fire it);
  // "Close application" then stays locked until CLOSE_UNLOCK_S after the
  // download began (downloadStartedAt), because a browser download reports no
  // completion event we could wait on. canClose flips true when that elapses.
  autoDownloaded: false,
  canClose: false,
  downloadStartedAt: null,

  // Settings (v2 §4.10) + Security (v2 §9)
  settingsKey: 'classifier',
  settingsContent: '',
  settingsEditOpen: false, // the expanded editor modal, opened from the doctrine's summary tile
  apiKeyPresent: false,
  invoiceLibrary: { dir_ready: false, csv_ready: false },

  // Guide (v2 §4.11)
  guideKey: 'getting_started',
  guideMarkdown: '',

  // Cross-cutting
  banner: null, // {kind: 'error'|'warn'|'info', text}
};

let eventsHandle = null;

function setState(patch) {
  // Banners are view-scoped: a navigation (any patch that actually changes
  // `view`) drops whatever banner was showing, so an error/info raised on one
  // screen doesn't keep following the operator around after they leave it.
  // Skipped when the same patch also sets a banner, so a call that navigates
  // AND reports something (e.g. goToForecast's failure path) isn't clobbered.
  if (patch.view !== undefined && patch.view !== state.view && !('banner' in patch)) {
    patch = { ...patch, banner: null };
  }
  Object.assign(state, patch);
  render();
}

/** Mutate state without a re-render — for live-typing fields (word gauges,
 * settings editor) where a full re-render would steal focus/cursor position
 * out of the input the operator is actively typing in. Callers update the
 * one satellite DOM node they care about (e.g. a word counter) directly. */
function pokeState(patch) {
  Object.assign(state, patch);
}

// ---------------------------------------------------------------------
// Actions — the only thing screens call. Each either mutates local state,
// calls the adapter, or both.
// ---------------------------------------------------------------------

function showBanner(kind, text) {
  setState({ banner: { kind, text } });
}
function clearBanner() {
  if (state.banner) setState({ banner: null });
}

async function refreshServerState() {
  const s = await adapter.getState();
  pokeState({
    apiKeyPresent: s.api_key_present,
    invoiceLibrary: s.invoice_library,
  });
  return s;
}

async function refreshQuarters() {
  if (state.workbookPhase !== 'confirmed') return;
  try {
    const q = await adapter.getQuarters();
    setState({ quarters: q.quarters, quartersWarnings: q.warnings });
  } catch (err) {
    showBanner('error', `Could not read quarters: ${err.message}`);
  }
}

// -- OneDrive/SharePoint access (optional) --------------------------------
// A prior server process may already hold a cached token (see
// gna_server/routes_graph.py's is_connected() check) -- this reflects that
// on load, without requiring a fresh click every restart.
async function refreshGraphStatus() {
  try {
    const r = await adapter.getGraphStatus();
    const error = r.error || null;
    // Only touch state (and trigger render()'s full root.innerHTML rebuild,
    // which replays every CSS entrance animation on the page) when the
    // status actually changed -- otherwise a same-status poll every 2s would
    // re-render the whole app for nothing while "connecting".
    if (r.status !== state.oneDriveStatus || error !== state.oneDriveError) {
      setState({ oneDriveStatus: r.status, oneDriveError: error });
    }
    if (r.status === 'connecting') setTimeout(refreshGraphStatus, 2000);
  } catch (_err) {
    // Transient fetch failure -- keep polling on the same cadence instead of
    // silently stranding the UI on "Connecting..." forever.
    if (state.oneDriveStatus === 'connecting') setTimeout(refreshGraphStatus, 2000);
  }
}

async function connectOneDrive() {
  if (state.oneDriveStatus === 'connecting') return;
  setState({ oneDriveStatus: 'connecting', oneDriveError: null });
  try {
    await adapter.connectOneDrive();
  } catch (err) {
    setState({ oneDriveStatus: 'error', oneDriveError: err.message });
    return;
  }
  setTimeout(refreshGraphStatus, 1500);
}

// Deliberate shutdown from the Output screen's "Close application" button (after
// the confirm dialog). Asks the server to stop (POST /api/shutdown), then shows
// the terminal end screen and stops the liveness ping / activity beacon so
// nothing keeps hitting a server that's on its way down. A 409 ("a run is in
// progress") or any other error surfaces as a banner and leaves the app usable.
async function closeApplication() {
  if (state.closing) return;
  setState({ closing: true });
  try {
    await adapter.shutdownApp();
  } catch (err) {
    setState({ closing: false, closeConfirmOpen: false });
    showBanner('error', `Could not close the app: ${err.message}`);
    return;
  }
  // Stop polling BEFORE flipping to the end screen: once the server drains, the
  // ping would start failing and could otherwise race us to the timeout screen.
  if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
  setState({ sessionClosed: true, closeConfirmOpen: false, closing: false });
}

// "Reset credentials" (Settings → Security): wipe the API key + Graph IDs from
// System/.env and stop the server, so the next launch runs first-time setup
// fresh. Parallels closeApplication() exactly — same graceful shutdown, same
// stop-polling-then-show-the-end-screen dance — but sets closedReason so the
// end screen shows reset-specific copy. A 409 (run in progress) or any other
// error surfaces as a banner and leaves the app usable.
async function resetCredentials() {
  if (state.resetting) return;
  setState({ resetting: true });
  try {
    await adapter.resetCredentials();
  } catch (err) {
    setState({ resetting: false, resetConfirmOpen: false });
    showBanner('error', `Could not reset credentials: ${err.message}`);
    return;
  }
  if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
  setState({ sessionClosed: true, closedReason: 'reset', resetConfirmOpen: false, resetting: false });
}

// A transient <a download> click (rather than navigating the tab) triggers a
// browser download without disturbing whatever screen is currently showing.
// Shared by the run output's workbook download and the Guide's sample-template
// downloads below — the two differ only in what happens after (see downloadWorkbook).
function triggerFileDownload(url) {
  const a = document.createElement('a');
  a.href = url;
  a.download = '';
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// The Output screen auto-downloads the workbook, and the operator can also click
// Download anytime. Both paths go through here, so the close-unlock countdown
// starts on whichever fires first.
function downloadWorkbook(key) {
  triggerFileDownload(adapter.downloadUrl(key));
  beginCloseUnlock();
}

// The Guide's sample-template downloads (Getting Started tab). Unlike the run
// output's classified.xlsx — a file the server just produced and we know is
// there — these hit a static repo file by key, so the request can genuinely
// fail (e.g. a retired/renamed key against a not-yet-restarted server). A blind
// <a download> would happily save whatever came back, including a 404's JSON
// body, as a file named like a spreadsheet — the operator then opens "an .xlsx"
// that Excel refuses. So fetch it, verify it's actually OK, and only then hand
// the browser a real Blob to save; on any failure, say so in a banner.
async function downloadSampleFile(key) {
  try {
    const res = await fetch(adapter.downloadUrl(key), { cache: 'no-store' });
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try { const body = await res.json(); if (body && body.detail) detail = body.detail; } catch (_e) { /* not JSON */ }
      throw new Error(detail);
    }
    const blob = await res.blob();
    // FastAPI's FileResponse sends Content-Disposition: attachment; filename="…";
    // honor it so the saved file keeps its real name, falling back to the key.
    const cd = res.headers.get('content-disposition') || '';
    const match = /filename="([^"]+)"/i.exec(cd) || /filename=([^;]+)/i.exec(cd);
    const filename = match ? match[1].trim() : `${key}.xlsx`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Revoke later, not synchronously: some browsers cancel the download if the
    // object URL is freed before the click's save has claimed it.
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (err) {
    showBanner('error', `Could not download the sample workbook: ${err.message}`);
  }
}

// "Close application" stays locked for CLOSE_UNLOCK_S after the download begins,
// giving the file time to actually land on disk (see downloadWorkbook: there's
// no completion event to wait on). A live countdown updates the hint in place so
// the Output screen's completion animation doesn't replay every second.
const CLOSE_UNLOCK_S = 20;
let closeUnlockTimer = null;
function beginCloseUnlock() {
  if (state.canClose || closeUnlockTimer) return; // already unlocked or counting
  const startedAt = Date.now();
  pokeState({ downloadStartedAt: startedAt });
  closeUnlockTimer = setInterval(() => {
    const remaining = Math.max(0, CLOSE_UNLOCK_S - Math.floor((Date.now() - startedAt) / 1000));
    const hint = document.getElementById('close-hint');
    if (hint) hint.textContent = `You can close the app in ${remaining}s…`;
    if (remaining <= 0) {
      clearInterval(closeUnlockTimer);
      closeUnlockTimer = null;
      pokeState({ canClose: true });
      // Flip the button in place (no setState) so the completion checkmark
      // doesn't re-animate; any later re-render reads canClose and stays correct.
      const btn = document.getElementById('close-btn');
      if (btn) { btn.disabled = false; btn.removeAttribute('aria-disabled'); }
      const lock = document.getElementById('close-lock');
      if (lock) lock.remove();
    }
  }, 1000);
}

// Re-arm the gate for a fresh run: stop any countdown and clear the latches, so
// the next completed run auto-downloads again and re-locks Close for 20s.
function resetCloseGate() {
  if (closeUnlockTimer) { clearInterval(closeUnlockTimer); closeUnlockTimer = null; }
  pokeState({ canClose: false, autoDownloaded: false, downloadStartedAt: null });
}

const actions = {
  // -- Hero --
  enterMain: () => { if (!state.heroEntered) setState({ heroEntered: true }); },

  // -- Header nav --
  goHome: () => setState({ view: 'main' }),
  // Guide/Settings are reachable from each other (the header stays visible on
  // both), so `prevView` must anchor to the screen you were on BEFORE you
  // started hopping between them -- if you're already on Guide or Settings,
  // keep the existing anchor instead of overwriting it with the other one.
  // Otherwise Guide -> Settings -> Back -> Back gets stuck bouncing between
  // the two and never reaches Home/Launch.
  openGuide: () => {
    adapter.getDoc(state.guideKey).then((r) => {
      const anchor = (state.view === 'guide' || state.view === 'settings') ? state.prevView : state.view;
      setState({ prevView: anchor, view: 'guide', guideMarkdown: r.markdown });
    }).catch((err) => showBanner('error', `Could not open the Guide: ${err.message}`));
  },
  openSettings: () => {
    adapter.getSettings(state.settingsKey).then((r) => {
      const anchor = (state.view === 'guide' || state.view === 'settings') ? state.prevView : state.view;
      setState({ prevView: anchor, view: 'settings', settingsContent: r.content, settingsEditOpen: false });
    });
  },
  goBack: () => setState({ view: state.prevView || 'main' }),
  goLaunch: () => setState({ view: 'launch' }),

  // -- Q2 two-file dropzones (G&A + A&T -> flatten). `slot` is 'ga' | 'at',
  //    read off data-slot; when BOTH slots hold a file, maybeFlattenQ2 fires. --
  onQ2Browse: (event, ds) => {
    const filled = ds.slot === 'ga' ? gaFileObj : atFileObj;
    if (filled || state.workbookPhase === 'checking') return; // remove to change; no-op while flattening
    document.getElementById(`q2-input-${ds.slot}`)?.click();
  },
  onQ2File: (event, ds) => {
    const file = event.target.files && event.target.files[0];
    if (file) setQ2Slot(ds.slot, file);
  },
  onQ2Drop: (event, ds, el) => {
    event.preventDefault();
    el?.classList.remove('dropzone--drag');
    pokeState(ds.slot === 'ga' ? { dragActiveGa: false } : { dragActiveAt: false });
    if (state.workbookPhase === 'checking') return;
    const file = event.dataTransfer?.files?.[0];
    if (file) setQ2Slot(ds.slot, file);
  },
  onQ2DragOver: (event) => event.preventDefault(),
  // Toggle the drag-highlight by mutating the DOM directly (no setState) --
  // the native dragenter/dragleave pair fires repeatedly as the pointer
  // crosses every child element inside the dropzone (the icon, the title,
  // the sub-label) while a single drag gesture is in progress. Routing that
  // through setState's full root.innerHTML rebuild replayed every CSS
  // entrance animation on the page dozens of times a second -- a jarring,
  // genuinely photosensitivity-risky flash. pokeState still keeps state in
  // sync (for the class computed on the next real render) without forcing one.
  onQ2DragEnter: (event, ds, el) => {
    event.preventDefault();
    el?.classList.add('dropzone--drag');
    pokeState(ds.slot === 'ga' ? { dragActiveGa: true } : { dragActiveAt: true });
  },
  onQ2DragLeave: (event, ds, el) => {
    event.preventDefault();
    el?.classList.remove('dropzone--drag');
    pokeState(ds.slot === 'ga' ? { dragActiveGa: false } : { dragActiveAt: false });
  },
  removeQ2: (event, ds) => { event.stopPropagation(); clearQ2Slot(ds.slot); },
  continueToLaunch: () => setState({ view: 'launch' }),

  // -- Launch: Additional Context modal (v2 §4.3) --
  openCtx: () => setState({ ctxOpen: true, ctxError: '', ctxOverCap: wordCount(state.userDealContext) > WORD_MAX }),
  closeCtx: () => setState({ ctxOpen: false }),
  onCtxInput: (event) => {
    const text = limitContextChars(event.target.value);
    pokeState({ userDealContext: text, ctxOverCap: wordCount(text) > WORD_MAX });
    updateCtxSatellites();
  },
  onCtxBrowse: () => document.getElementById('ctx-file-input')?.click(),
  onCtxFile: (event) => readContextFile(event.target.files?.[0]),
  onCtxDrop: (event) => { event.preventDefault(); readContextFile(event.dataTransfer?.files?.[0]); },
  onDragOver: (event) => event.preventDefault(),

  // -- Launch: Invoices dropzone (v2 §4.3/§6.3) — a folder can be supplied
  //    either by browsing (webkitdirectory input, flattens to a FileList on
  //    its own) or by dragging it onto the tile (needs the FileSystemEntry
  //    walk below — a dropped folder never shows up in dataTransfer.files). --
  onInvoiceBrowse: () => document.getElementById('invoice-input')?.click(),
  onInvoiceFolderBrowse: (event) => { event.stopPropagation(); document.getElementById('invoice-folder-input')?.click(); },
  onInvoiceFiles: (event) => { void uploadInvoices([...(event.target.files || [])]); },
  onInvoiceDrop: (event) => {
    event.preventDefault();
    void collectDroppedFiles(event.dataTransfer).then(uploadInvoices);
  },

  // -- Configure modal (v2 §4.4) --
  openConfig: () => setState({ configOpen: true }),
  closeConfig: () => setState({ configOpen: false }),
  stopPropagation: (event) => event.stopPropagation(),
  onQuarterSelect: (event) => pokeState({ selectedQuarter: event.target.value }),
  onMinUsdInput: (event) => pokeState({ minUsd: event.target.value === '' ? null : Number(event.target.value) }),
  saveConfig: () => {
    if (!state.selectedQuarter && state.quarters.length) {
      pokeState({ selectedQuarter: state.quarters[state.quarters.length - 1].label });
    }
    setState({ configOpen: false, configured: true });
  },

  // -- Launch: OneDrive access (optional) --
  connectOneDrive: () => { void connectOneDrive(); },
  toggleOneDriveAck: () => {
    const checked = !state.oneDriveAck;
    setState({ oneDriveAck: checked, oneDriveAckMissing: false });
    if (checked) clearBanner();
  },

  // -- Launch: Run --
  runBtnClick: () => {
    const oneDriveOk = state.oneDriveStatus === 'connected' || state.oneDriveAck;
    if (!state.configured) {
      showBanner('error', 'Configure the quarter and materiality threshold before running.');
      return;
    }
    if (!oneDriveOk) {
      setState({ oneDriveAckMissing: true });
      showBanner('error', 'Check the OneDrive acknowledgment box below, or connect OneDrive, before running.');
      return;
    }
    void goToForecast();
  },

  // -- Forecast + money gate (v2 §4.6) --
  toggleReady: () => setState({ readyChecked: !state.readyChecked }),
  proceedRun: () => {
    if (!state.confirmId) return;
    void adapter.confirmRun(state.confirmId, true);
    setState({ view: 'process', confirmId: null, reattaching: false });
  },
  cancelForecast: () => {
    if (state.confirmId) void adapter.confirmRun(state.confirmId, false);
    setState({ view: 'launch', reattaching: false });
  },

  // -- Process / live run (v2 §4.7) --
  stopRun: () => { void adapter.cancelRun(); },
  backToLaunchFromProcess: () => setState({ view: 'launch' }),

  // -- Output (v2 §4.8) --
  downloadArtifact: (event, dataset) => { downloadWorkbook(dataset.key); },
  recoverNow: async () => {
    resetCloseGate();
    const r = await adapter.startRun({ kind: 'recover', quarter: state.selectedQuarter });
    setState({ runId: r.run_id, runKind: 'recover', view: 'process', phase: null, liveRows: [], tally: {} });
    watchRun();
  },
  backToLaunchFromOutput: () => { resetCloseGate(); setState({ view: 'launch', runState: 'idle', runId: null }); },

  // -- Output: Close application (stops the local server) --
  closeApp: () => setState({ closeConfirmOpen: true }),
  cancelCloseApp: () => { if (!state.closing) setState({ closeConfirmOpen: false }); },
  confirmCloseApp: () => { void closeApplication(); },

  // -- Settings (v2 §4.10) --
  selectSettingsTab: (event, dataset) => {
    adapter.getSettings(dataset.key).then((r) => setState({ settingsKey: dataset.key, settingsContent: r.content }));
  },
  openSettingsEdit: () => setState({ settingsEditOpen: true }),
  closeSettingsEdit: () => setState({ settingsEditOpen: false }),

  // -- Settings: Reset credentials (wipes .env + stops the server) --
  openResetCredentials: () => setState({ resetConfirmOpen: true, resetAck: false }),
  cancelResetCredentials: () => { if (!state.resetting) setState({ resetConfirmOpen: false }); },
  toggleResetAck: (event) => {
    // Reflect the lever WITHOUT a full re-render. A setState here rebuilds
    // root.innerHTML, which replays the modal's omFade/omPop entrance animation
    // (and the main-view's omFade behind it) on every flip -- the "aggressive
    // reanimation". The CSS switch already slides from the input's native
    // :checked state, so we only poke state + flip the Reset button's
    // disabled/aria here, the same satellite-update pattern onSettingsInput uses
    // for the doctrine editor. state stays in sync for any later real render.
    const armed = event.target.checked;
    pokeState({ resetAck: armed });
    event.target.setAttribute('aria-checked', String(armed));
    const btn = document.getElementById('reset-confirm-btn');
    if (btn) btn.disabled = !armed;
  },
  confirmResetCredentials: () => { void resetCredentials(); },
  onSettingsInput: (event) => pokeState({ settingsContent: event.target.value }),
  saveSettings: async () => {
    try {
      await adapter.putSettings(state.settingsKey, state.settingsContent);
      setState({ settingsEditOpen: false });
      showBanner('info', 'Saved — this is now the live doctrine every run reads.');
    } catch (err) {
      showBanner('error', `Could not save: ${err.message}`);
    }
  },

  // -- Guide (v2 §4.11) --
  // A failed getDoc (a retired key against a not-yet-restarted server, a
  // transient error) used to reject silently — the .then never ran, setState
  // never fired, and the tab click looked completely dead to the operator.
  // Surface it as a banner so a genuinely broken section says so.
  selectGuideTab: (event, dataset) => {
    adapter.getDoc(dataset.key)
      .then((r) => setState({ guideKey: dataset.key, guideMarkdown: r.markdown }))
      .catch((err) => showBanner('error', `Could not load that section: ${err.message}`));
  },
  // Getting Started tab: the two sample workbook templates. Routed through
  // downloadSampleFile (fetch → verify → blob) rather than a blind anchor click,
  // so a failed request surfaces as a banner instead of saving the error
  // response as a file named .xlsx — exactly what "the download isn't a usable
  // Excel file" was, against a stale backend that 404'd the new download key.
  downloadSample: (event, dataset) => { void downloadSampleFile(dataset.key); },

  dismissBanner: clearBanner,
};

// ---------------------------------------------------------------------
// Async helpers backing the actions above
// ---------------------------------------------------------------------

// The two raw File objects live outside `state` (Files aren't render data and
// don't survive a re-render round-trip); `state.gaFile`/`state.atFile` hold
// just the {name} for display. When both are present, flatten fires.
let gaFileObj = null;
let atFileObj = null;

function setQ2Slot(slot, file) {
  if (slot === 'ga') { gaFileObj = file; setState({ gaFile: { name: file.name } }); }
  else { atFileObj = file; setState({ atFile: { name: file.name } }); }
  void maybeFlattenQ2();
}

function clearQ2Slot(slot) {
  if (slot === 'ga') gaFileObj = null; else atFileObj = null;
  // Removing either input invalidates the flattened result and any config
  // derived from it (its quarters).
  setState({
    [slot === 'ga' ? 'gaFile' : 'atFile']: null,
    workbookPhase: 'empty', workbookName: '', workbookChecks: [], workbookRowCount: 0,
    flattenSummary: null, quarters: [], configured: false, selectedQuarter: null,
  });
}

async function maybeFlattenQ2() {
  if (!gaFileObj || !atFileObj) return;      // need both files before flattening
  if (state.workbookPhase === 'checking') return; // a flatten is already in flight
  setState({
    workbookPhase: 'checking', workbookName: 'q2_flat.xlsx',
    configured: false, selectedQuarter: null,
  });
  try {
    const r = await adapter.uploadWorkbookQ2(gaFileObj, atFileObj);
    // A 200 means the pair flattened, NOT that the result is usable. Only
    // advance to 'confirmed' when EVERY validation check passed and it has
    // rows — a bad pair (wrong files, no MRI tabs) comes back with failed
    // checks and must stop here as an error, never a green "validated".
    const usable = r.checks.every((c) => c.ok) && r.row_count > 0;
    setState({
      workbookPhase: usable ? 'confirmed' : 'error',
      workbookName: r.name,
      workbookChecks: r.checks,
      workbookRowCount: r.row_count,
      hasExistingClassifications: r.has_existing_classifications,
      flattenSummary: r.flatten || null,
    });
    if (usable) await refreshQuarters();
  } catch (err) {
    setState({ workbookPhase: 'error', workbookChecks: [], flattenSummary: null });
    showBanner('error', `Flatten failed: ${err.message}`);
  }
}

function isPdfFile(file) { return /\.pdf$/i.test(file.name || ''); }

async function uploadInvoices(files) {
  // A folder (browsed or dropped) brings along whatever else lives next to
  // the invoices (readmes, .DS_Store, spreadsheets, subfolders of the same);
  // drop those silently here rather than round-tripping each one to the
  // server just to get "not a .pdf file" back.
  const pdfFiles = files.filter(isPdfFile);
  if (!pdfFiles.length) return;
  try {
    const r = await adapter.uploadInvoices(pdfFiles);
    const merged = [
      ...state.invoiceFiles,
      ...r.saved.map((name) => ({ name, ok: true })),
      ...r.rejected.map((x) => ({ name: x.name, ok: false, reason: x.reason })),
    ];
    setState({ invoiceFiles: merged });
  } catch (err) {
    showBanner('error', `Invoice upload failed: ${err.message}`);
  }
}

// A dropped FOLDER never appears in dataTransfer.files (that FileList only
// ever holds actual files) -- reading one requires the FileSystemEntry API:
// walk each dropped entry, recursing into directories, and collect every
// nested File. Falls back to the flat file list on a browser without
// webkitGetAsEntry (dropped files still work; a dropped folder just won't).
async function collectDroppedFiles(dataTransfer) {
  const items = dataTransfer?.items;
  if (!items || !items.length || typeof items[0].webkitGetAsEntry !== 'function') {
    return [...(dataTransfer?.files || [])];
  }
  const entries = [...items].map((item) => item.webkitGetAsEntry()).filter(Boolean);
  const out = [];
  await Promise.all(entries.map((entry) => walkEntry(entry, out)));
  return out;
}

function walkEntry(entry, out) {
  return new Promise((resolve) => {
    if (entry.isFile) {
      entry.file((file) => { out.push(file); resolve(); }, () => resolve());
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      // readEntries() returns AT MOST ~100 entries per call by spec -- must
      // keep calling until it reports an empty batch, or a large folder
      // silently loses everything past the first page.
      const readNextBatch = () => {
        reader.readEntries((batch) => {
          if (!batch.length) { resolve(); return; }
          Promise.all(batch.map((child) => walkEntry(child, out))).then(readNextBatch);
        }, () => resolve());
      };
      readNextBatch();
    } else {
      resolve();
    }
  });
}

// WORD_MAX is a soft guide, not an enforced ceiling -- the operator can type
// or paste past it; going over just surfaces a dilution warning (see
// updateCtxSatellites/context-modal.js) instead of blocking input. CHAR_MAX
// is the real hard backstop, set far above WORD_MAX so it only guards against
// a pathological paste (e.g. an entire document dropped in), never the
// ordinary case of running somewhat over the soft cap.
const WORD_MAX = 3500;
const CHAR_MAX = 100000;
function wordCount(str) { return str.trim() ? str.trim().split(/\s+/).length : 0; }
function limitContextChars(str) { return str.slice(0, CHAR_MAX); }
function readContextFile(file) {
  if (!file) return;
  pokeState({ ctxError: '' });
  const ext = (file.name.split('.').pop() || '').toLowerCase();
  if (ext === 'docx') {
    // A .docx is binary (a zip of XML) — the browser can't readAsText it. The
    // server extracts the text in memory (nothing written to disk) and returns
    // it; the result is folded into the session-only context, same as typing.
    adapter.extractContext(file)
      .then((r) => mergeContextText(String(r.text || '')))
      .catch((err) => setState({ ctxError: `Could not read that document: ${err.message}` }));
    return;
  }
  const reader = new FileReader();
  reader.onload = () => mergeContextText(String(reader.result || ''));
  reader.onerror = () => setState({ ctxError: 'Could not read that file.' });
  reader.readAsText(file);
}

function mergeContextText(text) {
  const combined = limitContextChars((state.userDealContext ? state.userDealContext + '\n' : '') + text);
  setState({ userDealContext: combined, ctxError: '', ctxOverCap: wordCount(combined) > WORD_MAX });
}
function updateCtxSatellites() {
  const count = wordCount(state.userDealContext);
  const over = count > WORD_MAX;
  const counter = document.getElementById('ctx-word-count');
  if (counter) {
    counter.textContent = `${count} / ${WORD_MAX} words`;
    counter.className = `word-gauge${over ? ' word-gauge--over' : ''}`;
  }
  const textarea = document.getElementById('ctx-textarea');
  if (textarea && textarea.value !== state.userDealContext) textarea.value = state.userDealContext;
  const warning = document.getElementById('ctx-dilute-warning');
  if (warning) warning.style.display = over ? 'flex' : 'none';
}

async function goToForecast() {
  resetCloseGate();
  setState({ view: 'forecast', runState: 'starting', sweepForecast: null, classifyForecast: null, readyChecked: false, confirmId: null });
  try {
    const r = await adapter.startRun({
      kind: 'run',
      quarter: state.selectedQuarter,
      min_usd: state.minUsd,
      user_deal_context: state.userDealContext || null,
    });
    setState({ runId: r.run_id, runKind: 'run' });
    watchRun();
  } catch (err) {
    setState({ view: 'launch' });
    showBanner('error', `Could not start the run: ${err.message}`);
  }
}

function watchRun() {
  if (eventsHandle) eventsHandle.close();
  eventsHandle = adapter.streamRunEvents(0, handleRunEvent);
}

/** Rebuild the live view for a run that was already in flight when the page
 * loaded (v2 §5's refresh-mid-run replay). Reset the per-run state, mark the
 * reattach in progress, land on the process screen, and let the SSE buffer
 * replay drive the rest (handleRunEvent's reattach block corrects the screen
 * to forecast if the run turns out to be sitting at the money gate). */
function reattachRun(run) {
  setState({
    reattaching: true,
    runKind: run.kind,
    runId: run.run_id,
    runState: 'running',
    view: 'process',
    liveRows: [], tally: {}, phase: null, statusSnapshot: null,
    sweepForecast: null, classifyForecast: null, phase0Stats: null,
    confirmId: null, readyChecked: false,
  });
  watchRun();
}

function handleRunEvent(event) {
  const p = event.payload;
  // Refresh-mid-run reattach only: GET /api/state tells us a run is active and
  // its kind, but not whether it's at the money gate or already past it. Infer
  // the screen from the replayed events (processed in seq order, last wins): a
  // pending confirm ⇒ forecast, live progress ⇒ process. Isolated behind
  // `reattaching` (cleared on the operator's first action / any terminal
  // state) so the normal forward flow never has its view second-guessed.
  if (state.reattaching) {
    if (event.type === 'confirm_request') {
      if (state.view !== 'forecast') pokeState({ view: 'forecast' });
    } else if (event.type === 'status' || event.type === 'row' ||
               (event.type === 'data' && p.kind === 'phase')) {
      if (state.view !== 'process') pokeState({ view: 'process' });
    }
  }
  switch (event.type) {
    case 'run_state':
      setState({ runState: p.state, runMessage: p.message || '' });
      if (p.state === 'done' || p.state === 'declined' || p.state === 'interrupted' || p.state === 'error') {
        pokeState({ reattaching: false });
        const onLiveScreen = state.view === 'process' || state.view === 'forecast';
        if ((state.runKind === 'run' || state.runKind === 'recover') && p.state === 'done') {
          // Always load results; only force-navigate to Output if the operator is
          // still on a live screen — don't yank them out of Settings/Guide/etc.
          void adapter.getResults().then((r) => {
            const hasArtifact = (r.artifacts || []).some((a) => a.key === 'classified');
            setState(
              onLiveScreen
                ? { summary: r.summary, artifacts: r.artifacts, view: 'output' }
                : { summary: r.summary, artifacts: r.artifacts }
            );
            // Auto-download the finished workbook once (the operator can still
            // click Download too). Latched by autoDownloaded so a later
            // re-render or reattach can't re-fire it; downloadWorkbook also
            // starts the 20s close-unlock countdown.
            if (hasArtifact && !state.autoDownloaded) {
              pokeState({ autoDownloaded: true });
              downloadWorkbook('classified');
            }
          });
          if (!onLiveScreen) showBanner('info', 'Run complete — open Output to download the workbook.');
        } else if (state.runKind === 'deal-profile' && p.state === 'done') {
          // This UI never starts a standalone deal-profile build (the Q2 flow's
          // single run builds the profile internally). This branch only fires
          // for a reattach to a deal-profile run started elsewhere (e.g. the
          // CLI) while the page was loading: cmd_deal_profile emits no
          // phase/'done' data events, so without it the process screen would sit
          // with the Stop button forever. Report success + refresh the quarters.
          setState({ view: 'launch' });
          refreshQuarters();
          showBanner('info', 'Deal profile built.');
        } else if (p.state === 'declined' || p.state === 'interrupted') {
          // stay on the current screen; the banner below explains what happened
          showBanner(p.state === 'interrupted' ? 'warn' : 'info', p.state === 'interrupted'
            ? 'Stopped — in-flight batches finished; whatever was already decided is durable.'
            : 'Declined — no paid call was made.');
        } else if (p.state === 'error') {
          showBanner('error', p.message || 'The run failed.');
        }
      }
      break;
    case 'data':
      if (p.kind === 'sweep_forecast') setState({ sweepForecast: p.payload });
      else if (p.kind === 'classify_forecast') setState({ classifyForecast: p.payload });
      else if (p.kind === 'phase0_stats') setState({ phase0Stats: p.payload });
      else if (p.kind === 'phase') setState({ phase: p.payload.phase });
      break;
    case 'status':
      setState({ statusSnapshot: p.snapshot || null });
      break;
    case 'row': {
      const rows = [p, ...state.liveRows].slice(0, 12);
      const tally = { ...state.tally };
      tally[p.classification] = (tally[p.classification] || 0) + 1;
      // Patch #live-rows directly when it's already on screen -- a row lands
      // every few seconds throughout a run, and setState()'s full
      // root.innerHTML rebuild would replay .main-view's entrance animation
      // (and every icon's CSS animation under it) on each one, reading as the
      // whole panel flashing/shaking. Only the very first row (no container
      // yet -- rowsHtml renders nothing while liveRows is empty) needs the
      // full render to create it.
      const container = document.getElementById('live-rows');
      pokeState({ liveRows: rows, tally });
      if (container) container.innerHTML = rows.map(process_.renderRow).join('');
      else render();
      break;
    }
    case 'confirm_request':
      setState({ confirmId: p.confirm_id, confirmPrompt: p.prompt, runState: 'awaiting_confirm' });
      break;
    case 'info':
      // Live feedback (run_manager emits an 'info' event on Stop). Surfacing its
      // message makes process.js's "stopping…" banner appear the moment Stop is
      // clicked, instead of only when the run reaches the terminal 'interrupted'.
      if (p.msg) setState({ runMessage: p.msg });
      break;
    default:
      break;
  }
}

// ---------------------------------------------------------------------
// Event delegation — the ONE place DOM events turn into action calls.
// ---------------------------------------------------------------------

function dispatch(actionName, event, el) {
  const fn = actions[actionName];
  if (typeof fn === 'function') fn(event, el.dataset, el);
}

root.addEventListener('click', (event) => {
  const el = event.target.closest('[data-action]');
  if (el) dispatch(el.dataset.action, event, el);
});
root.addEventListener('change', (event) => {
  const el = event.target.closest('[data-onchange]');
  if (el) dispatch(el.dataset.onchange, event, el);
});
root.addEventListener('input', (event) => {
  const el = event.target.closest('[data-oninput]');
  if (el) dispatch(el.dataset.oninput, event, el);
});
root.addEventListener('drop', (event) => {
  const el = event.target.closest('[data-ondrop]');
  if (el) dispatch(el.dataset.ondrop, event, el);
});
root.addEventListener('dragover', (event) => {
  const el = event.target.closest('[data-ondragover]');
  if (el) dispatch(el.dataset.ondragover, event, el);
});
root.addEventListener('dragenter', (event) => {
  const el = event.target.closest('[data-ondragenter]');
  if (el) dispatch(el.dataset.ondragenter, event, el);
});
root.addEventListener('dragleave', (event) => {
  const el = event.target.closest('[data-ondragleave]');
  if (el) dispatch(el.dataset.ondragleave, event, el);
});
// Safety net: a drop that lands even a few pixels outside a registered
// dropzone (a gap in the flex layout, padding, whitespace between tiles) never
// hits an element carrying `data-ondrop`, so none of the delegated handlers
// above ever call preventDefault() for it. Without this, the browser's own
// default action for a dropped file takes over -- for a binary file like
// .xlsx that's silently downloading it to the Downloads folder, and it can
// also navigate the tab away from the app entirely. This always fires
// (window sees the event after `root`'s delegated listeners, whether or not
// one of them already matched and handled it), so no drop can ever slip
// through to that default action.
window.addEventListener('dragover', (event) => event.preventDefault());
window.addEventListener('drop', (event) => event.preventDefault());
// Wheel/mousemove drive the hero's "scroll or hover to enter" gesture.
root.addEventListener('wheel', (event) => { if (event.deltaY > 0) actions.enterMain(); }, { passive: true });
root.addEventListener('mousemove', () => actions.enterMain(), { once: true });
// Escape closes an open modal (a11y — modals also carry role="dialog"/aria-modal).
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  if (state.ctxOpen) actions.closeCtx();
  else if (state.configOpen) actions.closeConfig();
  else if (state.closeConfirmOpen) actions.cancelCloseApp();
  else if (state.resetConfirmOpen) actions.cancelResetCredentials();
  else if (state.settingsEditOpen) actions.closeSettingsEdit();
});

// ---------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------

function screenFor(view) {
  return {
    main: heroValidation,
    launch: launch,
    forecast: forecast,
    process: process_,
    output: output,
    settings: settings,
    guide: guide,
  }[view] || heroValidation;
}

function render() {
  // Terminal end states. sessionClosed (operator clicked "Close application")
  // is checked FIRST so its "you're all set" copy wins over the timeout copy if
  // a late failing ping also trips sessionTimedOut as the server drains.
  if (state.sessionClosed) {
    root.innerHTML = closed_.render(state);
    return;
  }
  // Once the server has idle-timed-out there's nothing left to talk to, so
  // replace the whole app with the calm "session timed out" notice rather than
  // let stale screens throw failed requests at a dead server.
  if (state.sessionTimedOut) {
    root.innerHTML = timeout_.render(state);
    return;
  }

  const screen = screenFor(state.view);
  const bannerHtml = state.banner ? `
    <div class="banner banner--${state.banner.kind}" role="alert">
      <span>${escapeHtml(state.banner.text)}</span>
      <button data-action="dismissBanner" class="icon-btn" style="color:inherit" aria-label="Dismiss">&times;</button>
    </div>` : '';

  // While a run is actually in flight (money gate through the batch loop),
  // navigating away via the header would strand the operator off the Output
  // screen for the rest of the session (nothing routes back to it except the
  // run's own completion). Lock header nav so the only way off is Stop
  // (Process screen) or Cancel (Forecast's money gate) or waiting it out.
  const runLocked = state.runState === 'starting' || state.runState === 'running' || state.runState === 'awaiting_confirm';
  const lockAttr = runLocked ? 'disabled title="Finish or stop the current run before navigating away"' : '';

  root.innerHTML = `
    ${heroValidation.renderHero(state)}
    <div class="header-bar">
      <button data-action="goHome" class="header-logo" aria-label="Home" style="background:transparent;border:none;padding:0" ${lockAttr}>
        <img src="gnl-logo-stacked.png" alt="Global Net Lease" style="height:46px;width:auto;filter:brightness(0) invert(1)">
      </button>
      <div class="header-actions">
        <button data-action="openGuide" class="header-link-btn" ${lockAttr}>Guide</button>
        <button data-action="openSettings" class="icon-btn" aria-label="Settings" ${lockAttr}>${ICON_GEAR_HEADER}</button>
      </div>
    </div>
    <div style="width:100%;max-width:1000px;padding:0 24px;margin-top:14px">${bannerHtml}</div>
    ${screen.render(state)}
    ${state.configOpen ? configModal.render(state) : ''}
    ${state.ctxOpen ? contextModal.render(state) : ''}
    ${state.closeConfirmOpen ? renderCloseConfirm(state) : ''}
    ${state.resetConfirmOpen ? renderResetConfirm(state) : ''}
  `;

  if (state.ctxOpen) updateCtxSatellites();
  if (typeof screen.afterRender === 'function') screen.afterRender(state);
}

// "Close the application?" confirm dialog (Output screen). All copy is static
// (no user input), so nothing here needs escaping. Backdrop/Cancel both route
// to cancelCloseApp, which no-ops while a shutdown is already in flight.
function renderCloseConfirm(state) {
  const busy = state.closing;
  return `
    <div class="modal-overlay" data-action="cancelCloseApp">
      <div class="modal-panel modal-panel--confirm" data-action="stopPropagation" role="dialog" aria-modal="true" aria-label="Close the application">
        <div class="modal-title">Close the application?</div>
        <p class="modal-confirm-body">This shuts down the local server and clears the workbook and results from this session. Anything you've already downloaded stays safe on your computer.</p>
        <div class="modal-confirm-reminder">${ICON_WARNING}<span>Make sure your <strong>classified.xlsx</strong> download finished before closing.</span></div>
        <div class="modal-confirm-actions">
          <button data-action="cancelCloseApp" class="btn btn--plain" ${busy ? 'disabled' : ''}>Cancel</button>
          <button data-action="confirmCloseApp" class="btn btn--danger" ${busy ? 'disabled' : ''}>${busy ? 'Closing…' : 'Close application'}</button>
        </div>
      </div>
    </div>`;
}

// "Reset credentials?" confirm dialog (Settings → Security). Guards against an
// accidental click with a deliberate multi-step gate: the Security button never
// resets anything itself — it only opens THIS modal, which explains what will
// happen, and the destructive "Reset and close" button stays disabled until the
// operator flips the arming LEVER (a toggle switch, not a one-click control, so
// a stray click can't trip it). Only then does clicking Reset wipe the stored
// key/IDs and close the app. Copy is static, so nothing here needs escaping.
// Backdrop/Cancel/Escape all route to cancelResetCredentials, which no-ops while
// a reset is already in flight.
function renderResetConfirm(state) {
  const busy = state.resetting;
  const armed = state.resetAck; // the lever is flipped
  const canReset = armed && !busy;
  return `
    <div class="modal-overlay" data-action="cancelResetCredentials">
      <div class="modal-panel modal-panel--confirm" data-action="stopPropagation" role="dialog" aria-modal="true" aria-label="Reset credentials">
        <div class="modal-title">Reset credentials?</div>
        <p class="modal-confirm-body">Your Anthropic API key and Microsoft Graph tenant/client IDs are <strong>permanently deleted</strong> from this computer, and the app closes. Nothing else on your machine is touched. The next time you launch <strong>Start.cmd</strong>, the app has no credentials, so it walks you through entering them again from scratch.</p>
        <div class="modal-confirm-reminder">${ICON_WARNING}<span>This can't be undone — have your API key (and any Graph IDs) on hand before you continue.</span></div>
        <div class="ready-row" style="margin-top:14px">
          <div class="reset-lever-row">
            <label class="lever" title="${armed ? 'Reset is armed — flip back to cancel' : 'Flip to arm the reset'}">
              <input type="checkbox" id="reset-ack-checkbox" class="lever-input" role="switch" aria-checked="${armed}" aria-labelledby="reset-ack-label" ${armed ? 'checked' : ''} data-onchange="toggleResetAck">
              <span class="lever-track"><span class="lever-knob"></span></span>
            </label>
            <label id="reset-ack-label" for="reset-ack-checkbox" class="ready-copy">Flip this lever to confirm you understand — it's permanent, and you'll re-enter everything on the next launch. This unlocks the Reset button.</label>
          </div>
        </div>
        <div class="modal-confirm-actions">
          <button data-action="cancelResetCredentials" class="btn btn--plain" ${busy ? 'disabled' : ''}>Cancel</button>
          <button id="reset-confirm-btn" data-action="confirmResetCredentials" class="btn btn--danger" ${canReset ? '' : 'disabled'}>${busy ? 'Resetting…' : 'Reset and close'}</button>
        </div>
      </div>
    </div>`;
}

const ICON_GEAR_HEADER = `<svg width="22" height="22" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="3.2" stroke="currentColor" stroke-width="1.6"/><path d="M12 2.5v2.2M12 19.3v2.2M4.2 4.2l1.6 1.6M18.2 18.2l1.6 1.6M2.5 12h2.2M19.3 12h2.2M4.2 19.8l1.6-1.6M18.2 5.8l1.6-1.6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>`;

// escapeHtml now lives in ./screens/escape.js (single source, shared with the
// screen modules); re-exported here so any existing importer of it is unaffected.
export { escapeHtml };

// ---------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------

// Liveness ping — detect that the local server has idle-timed-out and shut
// itself down (gna_server/lifecycle.py, ~15 min of no activity), and show the
// calm "session timed out" screen instead of letting the next click fail.
//
// This polls /api/ping, which is DELIBERATELY exempt from the server's
// activity tracker (gna_server/app.py's _LIVENESS_PATHS) — so polling it does
// NOT keep the server alive and an open-but-idle tab still times out as
// intended. Genuine actions (uploads, configuring, running, downloading) hit
// normal endpoints that DO count as activity, so active use never times out.
// Two consecutive misses trip the screen (a single blip won't); once the
// server is truly gone the connection is refused immediately. A live server
// resets the counter.
const PING_MS = 20000;
let pingFails = 0;
let pingTimer = null;

async function pingLiveness() {
  // A deliberate close already stopped polling and showed the end screen; don't
  // resurrect a "timed out" screen from the failing pings the shutdown causes.
  if (state.sessionClosed) return;
  try {
    const res = await fetch('/api/ping', { cache: 'no-store' });
    pingFails = res.ok ? 0 : pingFails + 1;
  } catch (_e) {
    pingFails += 1; // connection refused once the server has stopped
  }
  if (pingFails >= 2) {
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
    if (!state.sessionTimedOut) setState({ sessionTimedOut: true });
  }
}
pingTimer = setInterval(pingLiveness, PING_MS);

// Keep-alive beacon — genuine on-screen activity must prevent a timeout, even
// when it makes no server request on its own: moving the mouse, typing in
// Additional Context, clicking tabs, scrolling while reading. We watch those
// DOM interactions and, at most once per BEACON_MS, tell the server "still
// here" (GET /api/activity resets the idle clock server-side). We stay SILENT
// when there's been no interaction since the last beacon, so a walked-away tab
// still idles out and shows the timeout screen. (Date.now here is ordinary
// browser JS.)
const BEACON_MS = 25000;
let lastInteraction = Date.now();
let lastBeacon = Date.now();
const markInteraction = () => { lastInteraction = Date.now(); };
// capture:true so a scroll inside the results table (scroll doesn't bubble to
// window) or an event some handler stops still registers as activity.
['mousemove', 'mousedown', 'keydown', 'wheel', 'scroll', 'touchstart', 'input']
  .forEach((evt) => window.addEventListener(evt, markInteraction, { passive: true, capture: true }));
setInterval(() => {
  if (state.sessionTimedOut || state.sessionClosed) return;
  if (lastInteraction > lastBeacon) {
    lastBeacon = Date.now();
    fetch('/api/activity', { cache: 'no-store' }).catch(() => {});
  }
}, BEACON_MS);

(async function boot() {
  render();
  void refreshGraphStatus();
  try {
    const s = await refreshServerState();
    if (s.run && s.run.active) {
      reattachRun(s.run); // page was refreshed mid-run — rebuild the live view
    } else {
      render();
    }
  } catch (err) {
    showBanner('error', `Could not reach the server: ${err.message}`);
  }
})();
