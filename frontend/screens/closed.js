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
//
// Two flavors, keyed off state.closedReason (set in app.js):
//   * default        — a plain "Close application" click (Output screen).
//   * 'reset'        — a "Reset credentials" click (Settings → Security): the
//                      stored API key + Graph IDs were deleted, so the copy
//                      tells the operator the next launch is a fresh setup.

export function render(state) {
  const reset = state && state.closedReason === 'reset';
  const title = reset ? 'Credentials cleared' : 'Your session has ended';
  const body = reset
    ? `Your Anthropic API key and Microsoft Graph IDs were deleted from this computer, and this session's data was cleared. Anything you'd already downloaded stays safe.`
    : `The application has closed and this session's data was cleared. Your downloaded workbook is safe on your computer — nothing else was kept.`;
  const relaunch = reset
    ? `You can close this browser tab. To use the app again, relaunch it by double-clicking <strong>Start.cmd</strong> — it will ask you to enter your API key (and any Graph IDs) from scratch.`
    : `You can close this browser tab. To start a new session, relaunch the app by double-clicking <strong>Start.cmd</strong>.`;
  return `
    <div class="timeout-screen" role="alertdialog" aria-labelledby="closed-title" aria-describedby="closed-body">
      <div class="timeout-card">
        <img src="gnl-logo-front.png" alt="Global Net Lease" class="timeout-logo">
        <h1 id="closed-title" class="timeout-title">${title}</h1>
        <p id="closed-body" class="timeout-body">${body}</p>
        <p class="timeout-body timeout-body--soft">${relaunch}</p>
      </div>
    </div>`;
}
