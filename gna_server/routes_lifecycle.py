"""routes_lifecycle.py — POST /api/shutdown and POST /api/reset-credentials.

/api/shutdown is the operator-initiated app close from the Output screen. The
idle watchdog (gna_server.lifecycle) already knows how to stop the server
gracefully after a stretch of inactivity; this exposes that same trigger to a
deliberate "Close application" click (frontend/screens/output.js). Stopping the
server is also what fires __main__'s atexit cleanup of the throwaway temp
workspace, so closing here clears the operator's uploaded workbook and results
too -- the frontend confirms first and reminds them their download is safe.

/api/reset-credentials wipes the stored credentials -- the Anthropic API key and
the optional Microsoft Graph tenant/client IDs -- from System/.env, then stops
the server the same graceful way. Two facts make the stop mandatory rather than
optional: a running server keeps the key in its own process environment no
matter what .env says, and the launcher (System/launch_ui.ps1) only ever
(re-)prompts for credentials at startup. So the wipe only "means" anything
paired with the restart that re-reads the now-blank .env -- the NEXT launch runs
first-time setup from scratch. Clearing the API key is what makes that first-run
block fire; that same block is also the only place the optional Graph IDs are
re-prompted for, which is why all three are cleared together (the frontend
explains this to the operator).

Both endpoints refuse while a run is active, mirroring the watchdog's own guard:
a live (paid) classification must never be killed by a stray shutdown.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from gna_pipeline import config

from . import lifecycle
from .run_manager import manager

router = APIRouter()

# The credentials a reset clears from System/.env.
_RESET_KEYS = ("ANTHROPIC_API_KEY", "GRAPH_TENANT_ID", "GRAPH_CLIENT_ID")

# The real, durable credential file the launcher loads at startup -- NOT
# anything under the ephemeral temp workspace. REPO_ROOT is stable regardless of
# GNA_DATA_ROOT, so this always points at the file launch_ui.ps1 reads.
# (routes_settings.py likewise writes real repo files from the UI, so a
# UI-triggered write outside the workspace is an established pattern here.)
_ENV_PATH = config.REPO_ROOT / "System" / ".env"


def _wipe_env_keys(keys) -> list[str]:
    """Blank the given KEY= values in System/.env IN PLACE, preserving every
    other line (comments, PATH, unrelated keys, spacing) exactly. Returns the
    keys whose line was actually found and blanked.

    A key that has no uncommented line is already "absent" as far as the
    launcher is concerned (Test-ApiKeyPresent / Test-ValuePresent both treat a
    missing or blank value as needs-setup), so it's simply skipped. Written
    UTF-8 without a BOM so python-dotenv and the PowerShell loader both read the
    first key cleanly on the next launch.
    """
    if not _ENV_PATH.is_file():
        return []
    patterns = {k: re.compile(r"^\s*" + re.escape(k) + r"\s*=") for k in keys}
    cleared: list[str] = []
    out: list[str] = []
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        matched = None
        if not line.lstrip().startswith("#"):
            for k, pat in patterns.items():
                if pat.match(line):
                    matched = k
                    break
        if matched is not None:
            out.append(f"{matched}=")
            if matched not in cleared:
                cleared.append(matched)
        else:
            out.append(line)
    _ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    return cleared


@router.post("/api/shutdown")
def shutdown() -> dict:
    if manager.is_active():
        raise HTTPException(409, "a run is in progress -- cancel it or let it finish before closing")
    if not lifecycle.request_shutdown():
        # No exit hook registered -- e.g. under pytest's TestClient, where there
        # is no uvicorn Server to stop. Report it rather than pretend.
        raise HTTPException(503, "server shutdown is unavailable in this context")
    return {"ok": True}


@router.post("/api/reset-credentials")
def reset_credentials() -> dict:
    """Wipe the stored credentials from System/.env and stop the server, so the
    next launch re-runs first-time setup. Refuses mid-run, same as shutdown."""
    if manager.is_active():
        raise HTTPException(409, "a run is in progress -- cancel it or let it finish before resetting")
    # Stop FIRST, wipe SECOND: only clear the stored credentials once we know the
    # server is genuinely going down (request_shutdown returned True). A running
    # server keeps the key in its process env regardless of .env, so wiping
    # without the accompanying restart would just leave an inconsistent file
    # behind. If there's no exit hook (TestClient) we wipe nothing and say so.
    if not lifecycle.request_shutdown():
        raise HTTPException(503, "server shutdown is unavailable in this context")
    cleared = _wipe_env_keys(_RESET_KEYS)
    return {"ok": True, "cleared": cleared}
