// closed.js — the terminal "you closed the app" screen.
//
// Shown after the operator clicks "Close application" on the Output screen and
// the local server has been asked to shut down (POST /api/shutdown ->
// gna_server/routes_lifecycle.py). Shutting the server down also clears the
// throwaway workspace (uploaded workbook + results), so this is a calm,
// explained end-state -- "you're done; your download is safe; relaunch to run
// again" -- and it is terminal, because the server is gone until relaunch.
//
// Deliberately parallel to timeout.js (idle auto-shutdown) and reuses its
// styling; only the copy differs, to make clear this was the operator's own
// choice rather than a timeout.

export function render(_state) {
  return `
    <div class="timeout-screen" role="alertdialog" aria-labelledby="closed-title" aria-describedby="closed-body">
      <div class="timeout-card">
        <img src="gnl-logo-front.png" alt="Global Net Lease" class="timeout-logo">
        <div class="timeout-badge">Session ended</div>
        <h1 id="closed-title" class="timeout-title">You're all set</h1>
        <p id="closed-body" class="timeout-body">
          The application has closed and this session's data was cleared. Your
          downloaded workbook is safe on your computer — nothing else was kept.
        </p>
        <p class="timeout-body timeout-body--soft">
          You can close this browser tab. To start a new session, relaunch the app
          by double-clicking <strong>Start.cmd</strong>.
        </p>
      </div>
    </div>`;
}
