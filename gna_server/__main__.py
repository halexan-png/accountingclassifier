"""python -m gna_server — start the local UI server on loopback.

Loads System/.env the same way the CLI does (ANTHROPIC_API_KEY, etc.) and binds
ONLY to 127.0.0.1 -- never 0.0.0.0 (v2 UI handoff §9). launch_ui.ps1 is the
operator-facing entry point (discovers Python, installs the `ui` extras on
first run, starts this, opens the browser); this module is what it execs.

Stateless by default: unless GNA_DATA_ROOT is explicitly set, the server runs
in a throwaway temp workspace (created at startup, deleted on exit), so a
freshly cloned repo works with zero setup and NO business data is ever written
into the repo tree or left behind on the operator's machine. Every input
arrives via UI upload; every result leaves via the UI's Download button. Data
ownership is entirely the operator's -- the app keeps nothing.

Self-stopping: a background watchdog (gna_server.lifecycle) shuts the server
down after GNA_UI_IDLE_TIMEOUT_MIN minutes (default 15) with no HTTP activity
and no run in progress, so a walked-away-from server doesn't linger -- and,
because shutdown triggers the atexit cleanup above, the operator's uploaded
data clears itself. Set GNA_UI_IDLE_TIMEOUT_MIN=0 to disable and run until
Ctrl+C / window close.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

# .env lives in System/ (repo_root/System/.env), one level below this package.
# launch_ui.ps1 already exports these vars before starting us, so this mainly
# covers a direct `python -m gna_server` dev run. load_dotenv does not override
# vars already in the environment, so the launcher's values still win.
_ENV_FILE = Path(__file__).resolve().parent.parent / "System" / ".env"

DEFAULT_PORT = 8420


def _use_ephemeral_workspace() -> None:
    """Point the whole session at a throwaway temp workspace unless the operator
    pinned GNA_DATA_ROOT on purpose. config.py reads GNA_DATA_ROOT at import
    time, and uvicorn.run() imports gna_server.app (hence config) only AFTER
    this runs, so setting the env var here makes every data path -- uploads,
    intermediate results, the classified workbook -- resolve into the temp dir.
    The directory is removed on normal interpreter exit; nothing durable is left
    on disk and nothing ever touches the repo tree.
    """
    if os.environ.get("GNA_DATA_ROOT", "").strip():
        return  # operator pinned a location on purpose -- respect it.
    workdir = tempfile.mkdtemp(prefix="gna_ui_")
    os.environ["GNA_DATA_ROOT"] = workdir
    atexit.register(shutil.rmtree, workdir, ignore_errors=True)


def main() -> None:
    load_dotenv(_ENV_FILE)
    _use_ephemeral_workspace()
    port_raw = os.environ.get("GNA_UI_PORT", "").strip()
    port = int(port_raw) if port_raw else DEFAULT_PORT

    # Build the Server explicitly (rather than uvicorn.run(), which constructs
    # and blocks in one call) so the idle watchdog can flip should_exit to stop
    # it. The app target stays a late-imported string so gna_server.app -- and
    # thus gna_pipeline.config, which reads GNA_DATA_ROOT at import -- is
    # imported only after _use_ephemeral_workspace() has set it above.
    server = uvicorn.Server(
        uvicorn.Config("gna_server.app:app", host="127.0.0.1", port=port, log_level="info")
    )

    # Import here, not at module top: run_manager pulls in gna_pipeline.config,
    # so importing it before _use_ephemeral_workspace() would freeze the data
    # root before the temp workspace is chosen.
    from . import lifecycle
    from .run_manager import manager

    # Both the idle watchdog and the operator's "Close application" button (POST
    # /api/shutdown) stop the server this same way -- flip should_exit so uvicorn
    # drains and the atexit cleanup above wipes the temp workspace. Register the
    # hook unconditionally: the watchdog may be disabled (GNA_UI_IDLE_TIMEOUT_MIN
    # =0) but the manual Close button must still work.
    def _request_exit() -> None:
        server.should_exit = True

    lifecycle.register_exit(_request_exit)
    lifecycle.start_watchdog(
        request_exit=_request_exit,
        is_run_active=manager.is_active,
        log=lambda msg: print(f"[gna] {msg}", flush=True),
    )

    server.run()


if __name__ == "__main__":
    main()
