"""lifecycle.py — idle auto-shutdown for the local UI server.

The UI is a single-operator local app started by double-clicking Start.cmd
(or launch_ui.ps1). Nothing external can reach it (loopback + Host/Origin
guard in app.py) and it costs nothing while idle, so leaving it running is
harmless -- EXCEPT that closing the server is also what wipes the ephemeral
temp workspace (__main__._use_ephemeral_workspace), i.e. the operator's
uploaded workbook and results. So rather than rely on the operator remembering
to close the window, a background watchdog shuts the process down after a
stretch of no HTTP activity AND no run in progress. Normal interpreter exit
then fires the atexit temp-workspace cleanup -- the data clears itself once
the operator is done.

Two things keep this from ever killing work in flight:
  * An active run never counts as idle -- the watchdog gates on
    run_manager.manager.is_active() (passed in as is_run_active), so a long
    classification run that makes no other HTTP calls for minutes is safe.
  * Every served request calls touch(); the frontend pings /api/state on a
    steady heartbeat while its tab is open (app.js), so an open-but-idle
    results screen stays "alive" and a reviewer reading it won't have the
    server vanish out from under a pending download. Closing the tab stops the
    heartbeat; the timeout then elapses and the server exits.

The timeout is GNA_UI_IDLE_TIMEOUT_MIN minutes (default 15); set it to 0 to
disable auto-shutdown entirely (the server then runs until Ctrl+C / window
close, the pre-existing behavior).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable

DEFAULT_IDLE_TIMEOUT_MIN = 15.0
# How often the watchdog wakes to re-check. Well under the timeout so the
# actual shutdown lands within one interval of the true idle deadline.
_CHECK_INTERVAL_S = 15.0

_lock = threading.Lock()
# Monotonic (not wall-clock) so a system clock change can't make the server
# think it's been idle for hours. Seeded at import so a server that never
# receives a request still eventually shuts down.
_last_activity = time.monotonic()


def touch() -> None:
    """Record 'a request just happened'. Called from app.py's middleware on
    every served request; resets the idle countdown."""
    global _last_activity
    with _lock:
        _last_activity = time.monotonic()


def seconds_since_activity(now: float | None = None) -> float:
    with _lock:
        last = _last_activity
    return (time.monotonic() if now is None else now) - last


def resolve_timeout_seconds() -> float:
    """Idle timeout in seconds from GNA_UI_IDLE_TIMEOUT_MIN.

    Unset/blank -> the 5-minute default. A positive number -> that many
    minutes. Zero or negative -> 0.0 (auto-shutdown disabled). An unparseable
    value falls back to the default rather than silently disabling the feature.
    """
    raw = os.environ.get("GNA_UI_IDLE_TIMEOUT_MIN", "").strip()
    if not raw:
        return DEFAULT_IDLE_TIMEOUT_MIN * 60.0
    try:
        minutes = float(raw)
    except ValueError:
        return DEFAULT_IDLE_TIMEOUT_MIN * 60.0
    if minutes <= 0:
        return 0.0
    return minutes * 60.0


def should_shut_down(*, timeout_s: float, idle_s: float, run_active: bool) -> bool:
    """Pure decision: shut down only if the feature is enabled, no run is
    active, and we've been idle at least the timeout. Kept side-effect-free so
    the policy is unit-testable without a running server or real clock."""
    if timeout_s <= 0:  # disabled
        return False
    if run_active:  # never interrupt a live run
        return False
    return idle_s >= timeout_s


# --------------------------------------------------------------------------
# Shared exit hook. Both the idle watchdog (below) and a deliberate operator
# "Close application" click (POST /api/shutdown -> routes_lifecycle.py) stop the
# server the SAME way: flip uvicorn's Server.should_exit so it drains in-flight
# requests and the process exits normally, which fires __main__'s atexit
# temp-workspace cleanup. __main__.main() registers the hook once at startup;
# anything wanting a graceful shutdown calls request_shutdown().
# --------------------------------------------------------------------------

_exit_lock = threading.Lock()
_request_exit_cb: Callable[[], None] | None = None


def register_exit(request_exit: Callable[[], None] | None) -> None:
    """Record how to stop the running server (a callback that flips uvicorn's
    should_exit). Called once from __main__; pass None to clear it (tests)."""
    global _request_exit_cb
    with _exit_lock:
        _request_exit_cb = request_exit


def request_shutdown() -> bool:
    """Trigger a graceful shutdown via the registered exit hook. Returns True if
    a hook was registered and invoked, False otherwise (e.g. under pytest's
    TestClient, where there is no uvicorn Server to stop) -- callers surface
    that honestly rather than pretend the server is going down."""
    with _exit_lock:
        cb = _request_exit_cb
    if cb is None:
        return False
    cb()
    return True


def start_watchdog(
    request_exit: Callable[[], None],
    *,
    is_run_active: Callable[[], bool],
    log: Callable[[str], None] | None = None,
) -> threading.Thread | None:
    """Start the idle watchdog on a daemon thread. Returns the thread, or None
    if auto-shutdown is disabled (GNA_UI_IDLE_TIMEOUT_MIN=0).

    request_exit is called once, from the watchdog thread, when the idle
    deadline is reached with no active run -- wire it to uvicorn's
    Server.should_exit so the server drains and the process exits normally.
    """
    timeout_s = resolve_timeout_seconds()
    if timeout_s <= 0:
        return None

    def _loop() -> None:
        while True:
            time.sleep(_CHECK_INTERVAL_S)
            if should_shut_down(
                timeout_s=timeout_s,
                idle_s=seconds_since_activity(),
                run_active=is_run_active(),
            ):
                minutes = timeout_s / 60.0
                if log is not None:
                    log(f"Idle for {minutes:g} min with no active run -- shutting the server down "
                        "and clearing the temporary workspace. Re-launch to run again.")
                request_exit()
                return

    thread = threading.Thread(target=_loop, name="gna-idle-watchdog", daemon=True)
    thread.start()
    return thread
