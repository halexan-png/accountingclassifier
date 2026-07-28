// timeout.js — the full-screen "session timed out" notice.
//
// Shown when the frontend's liveness ping (app.js) can no longer reach the
// server. The local server shuts itself down after ~15 min of inactivity
// (gna_server/lifecycle.py), which also clears the uploaded workbook and
// results from the throwaway workspace. This screen turns that into a calm,
// explained end-state -- "the app didn't break, it timed out; relaunch to
// start again" -- instead of a blank page or a failed request. It is terminal:
// there is no dismiss, because the server is gone until the operator relaunches.

export function render(_state) {
  return `
    <div class="timeout-screen" role="alertdialog" aria-labelledby="timeout-title" aria-describedby="timeout-body">
      <div class="timeout-card">
        <img src="gnl-logo-front.png" alt="Global Net Lease" class="timeout-logo">
        <div class="timeout-badge">Session ended</div>
        <h1 id="timeout-title" class="timeout-title">Your session timed out</h1>
        <p id="timeout-body" class="timeout-body">
          The application closed itself after <strong>15 minutes of inactivity</strong>
          and cleared the data from this session. Nothing went wrong — this is the
          normal, secure behavior that keeps your financial data from lingering.
        </p>
        <p class="timeout-body timeout-body--soft">
          Any workbook you already downloaded is safe on your computer. To start a
          new session, relaunch the app by double-clicking <strong>Start.cmd</strong>.
        </p>
      </div>
    </div>`;
}
