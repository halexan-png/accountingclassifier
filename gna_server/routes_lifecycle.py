"""routes_lifecycle.py — POST /api/shutdown: operator-initiated app close.

The idle watchdog (gna_server.lifecycle) already knows how to stop the server
gracefully after a stretch of inactivity; this exposes that same trigger to a
deliberate "Close application" click on the Output screen (frontend/screens/
output.js). Stopping the server is also what fires __main__'s atexit cleanup of
the throwaway temp workspace, so closing here clears the operator's uploaded
workbook and results too -- the frontend confirms first and reminds them their
download is already safe on disk.

Refuses while a run is active, mirroring the watchdog's own guard: a live
(paid) classification must never be killed by a stray shutdown.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import lifecycle
from .run_manager import manager

router = APIRouter()


@router.post("/api/shutdown")
def shutdown() -> dict:
    if manager.is_active():
        raise HTTPException(409, "a run is in progress -- cancel it or let it finish before closing")
    if not lifecycle.request_shutdown():
        # No exit hook registered -- e.g. under pytest's TestClient, where there
        # is no uvicorn Server to stop. Report it rather than pretend.
        raise HTTPException(503, "server shutdown is unavailable in this context")
    return {"ok": True}
