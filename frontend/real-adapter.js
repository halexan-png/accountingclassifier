// real-adapter.js — the PRODUCTION backend adapter (Wave 3).
//
// Implements EXACTLY the same interface as mock-adapter.js (see that file's
// header for the authoritative contract), but every method talks to
// gna_server's real REST/SSE endpoints over `fetch` / `EventSource` instead
// of scripted timers. Wave 3's "swap mock for real" is a one-line import
// change in app.js (`./mock-adapter.js` -> `./real-adapter.js`); no screen
// file changes, because every screen and action was written against this
// interface from the start (v2 UI handoff §7, HANDOFF_1B_frontend.md).
//
// Every request is same-origin (the frontend is served by the same FastAPI
// app on 127.0.0.1), so URLs are relative — no host, no CDN, no cross-origin.
// The server's loopback guard (gna_server/app.py) requires an
// application/json or multipart/form-data Content-Type on every
// state-changing request; this file always sends one.

// ---------------------------------------------------------------------
// fetch helpers
// ---------------------------------------------------------------------

/** fetch + JSON, turning any non-2xx into a thrown Error carrying the
 * server's `detail` string (FastAPI's HTTPException shape) so app.js's
 * try/catch banners show a real reason, not "[object Object]". */
async function jfetch(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && body.detail != null) {
        detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
      }
    } catch (_e) { /* error body wasn't JSON — keep the status line */ }
    throw new Error(detail);
  }
  const text = await res.text();
  return text ? JSON.parse(text) : {};
}

/** POST a JSON body (always sets the Content-Type the loopback guard needs). */
function jsonPost(url, body) {
  return jfetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  });
}

// Terminal run states (mirrors gna_server/run_manager.py's _TERMINAL_STATES).
// Once one arrives the run is over and the server closes the SSE stream; the
// adapter closes its EventSource so the browser does NOT auto-reconnect and
// replay the whole finished run again.
const TERMINAL_STATES = new Set(['done', 'declined', 'interrupted', 'error']);

// ---------------------------------------------------------------------
// Adapter surface — one method per app.js call, matching mock-adapter.js.
// ---------------------------------------------------------------------

export const adapter = {
  async getState() {
    return jfetch('/api/state');
  },

  // Q2 two-file upload: the multi-tab G&A workbook + the flat A&T workbook.
  // The server flattens them into one canonical sheet, validates THAT file,
  // and tracks it -- so getQuarters()/startRun() downstream are unchanged.
  async uploadWorkbookQ2(gaFile, atFile) {
    const fd = new FormData();
    fd.append('ga', gaFile, gaFile.name);
    fd.append('at', atFile, atFile.name);
    return jfetch('/api/workbook-q2', { method: 'POST', body: fd });
  },

  async getQuarters() {
    return jfetch('/api/quarters');
  },

  async uploadInvoices(files) {
    const fd = new FormData();
    for (const f of files) fd.append('files', f, f.name);
    return jfetch('/api/invoices', { method: 'POST', body: fd });
  },

  // Extract the plain text of a .txt/.md/.docx Additional-Context file. The
  // server reads it in memory and returns {name, text} — nothing is stored.
  async extractContext(file) {
    const fd = new FormData();
    fd.append('file', file, file.name);
    return jfetch('/api/context/extract', { method: 'POST', body: fd });
  },

  // Microsoft Graph (OneDrive/SharePoint invoice links) — optional. connect()
  // kicks off a background browser sign-in (gna_server/routes_graph.py);
  // getGraphStatus() is polled while it's in flight.
  async connectOneDrive() {
    return jsonPost('/api/graph/connect', {});
  },

  async getGraphStatus() {
    return jfetch('/api/graph/status');
  },

  async getSettings(key) {
    return jfetch(`/api/settings/${encodeURIComponent(key)}`);
  },

  async putSettings(key, content) {
    return jfetch(`/api/settings/${encodeURIComponent(key)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    });
  },

  async startRun(opts) {
    return jsonPost('/api/run', {
      kind: opts.kind,
      quarter: opts.quarter ?? null,
      deal_profile_quarters: opts.deal_profile_quarters ?? null,
      min_usd: opts.min_usd ?? null,
      user_deal_context: opts.user_deal_context ?? null,
    });
  },

  // SSE. Returns {close()} exactly like the mock. Replays the server's ring
  // buffer from `afterSeq` (app.js passes 0, so a refresh mid-run rebuilds the
  // whole live view), then streams live. Two things the mock never had to do,
  // both handled here so no screen code has to care:
  //   1. Close on a terminal run_state, so the server-side stream close does
  //      not make EventSource auto-reconnect and re-deliver the finished run.
  //   2. De-dupe by monotonic `seq` and reconnect FROM the last seq seen — the
  //      native EventSource would otherwise reconnect to the original
  //      `?after=` and replay already-processed events (handleRunEvent is not
  //      idempotent: `row` events accumulate a tally).
  streamRunEvents(afterSeq, onEvent) {
    let closed = false;
    let lastSeq = afterSeq || 0;
    let es = null;

    const open = () => {
      if (closed) return;
      es = new EventSource(`/api/run/events?after=${lastSeq}`);
      es.onmessage = (ev) => {
        let entry;
        try { entry = JSON.parse(ev.data); } catch (_e) { return; }
        if (typeof entry.seq === 'number') {
          if (entry.seq <= lastSeq) return; // already delivered — ignore replay
          lastSeq = entry.seq;
        }
        onEvent(entry);
        if (entry.type === 'run_state' && entry.payload && TERMINAL_STATES.has(entry.payload.state)) {
          closed = true;
          es.close();
        }
      };
      es.onerror = () => {
        // The server closes the stream when the run ends (expected — we've
        // already latched `closed` from the terminal event) OR the connection
        // dropped mid-run. Reconnect ourselves from lastSeq on a real drop;
        // native reconnect would re-request the ORIGINAL ?after= and replay.
        if (es) es.close();
        if (closed) return;
        setTimeout(open, 1000);
      };
    };

    open();
    return { close() { closed = true; if (es) es.close(); } };
  },

  // Void-ed by app.js (proceedRun/cancelForecast/stopRun ignore the result),
  // so a stale/unknown confirm_id (404) or a transient error must resolve to
  // {ok:false}, never an unhandled promise rejection.
  async confirmRun(confirmId, answer) {
    try {
      return await jsonPost('/api/run/confirm', { confirm_id: confirmId, answer });
    } catch (_e) {
      return { ok: false };
    }
  },

  async cancelRun() {
    try {
      return await jsonPost('/api/run/cancel', {});
    } catch (_e) {
      return { ok: false };
    }
  },

  // Deliberate "Close application" (Output screen). Stops the local server the
  // same graceful way the idle watchdog does (POST /api/shutdown ->
  // gna_server/routes_lifecycle.py), which also clears the throwaway workspace.
  // Once this resolves the server is on its way down; app.js shows the end
  // screen and stops polling so nothing else hits a dead server. NOT wrapped in
  // a swallow-all try/catch (unlike cancelRun): a 409 "a run is in progress"
  // must surface to the operator as a banner, so let the error propagate.
  async shutdownApp() {
    return jsonPost('/api/shutdown', {});
  },

  async getResults() {
    return jfetch('/api/results');
  },

  // A real URL app.js navigates to (window.location.href = ...). The server
  // responds with Content-Disposition: attachment, so the browser downloads
  // the file and stays on the page.
  downloadUrl(key) {
    return `/api/download/${encodeURIComponent(key)}`;
  },

  async getDoc(key) {
    return jfetch(`/api/docs/${encodeURIComponent(key)}`);
  },
};
