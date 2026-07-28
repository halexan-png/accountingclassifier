"""routes_graph.py — POST /api/graph/connect, GET /api/graph/status.

Optional Microsoft Graph (OneDrive/SharePoint invoice links) sign-in. Kept
disjoint from run_manager.py's SSE-driven run state on purpose: this is a
single one-time browser popup, not a multi-stage pipeline run, so a small
in-module status enum + background thread is all it needs — no event log,
no cancel, no confirm bridge.

connect_interactive() (gna_pipeline/graph_auth.py) blocks in its own thread
for as long as the operator takes in the browser; the frontend polls
/api/graph/status every couple seconds while "connecting".
"""

from __future__ import annotations

import threading
from typing import Literal

from fastapi import APIRouter

from gna_pipeline import graph_auth

router = APIRouter()

_GraphStatus = Literal["idle", "connecting", "connected", "error"]

_lock = threading.Lock()
_status: _GraphStatus = "idle"
_error: str | None = None


def _connect_worker() -> None:
    global _status, _error
    ok, err = graph_auth.connect_interactive()
    with _lock:
        _status = "connected" if ok else "error"
        _error = None if ok else err


@router.get("/api/graph/status")
def get_graph_status() -> dict:
    with _lock:
        status, error = _status, _error
    # A prior process already connected (token cache on disk) — reflect that
    # as "connected" without requiring a fresh click every server restart.
    if status == "idle" and graph_auth.is_connected():
        status = "connected"
    return {"status": status, "error": error}


@router.post("/api/graph/connect")
def connect_graph() -> dict:
    global _status, _error
    with _lock:
        if _status == "connecting":
            return {"status": _status, "error": None}
        _status = "connecting"
        _error = None
    thread = threading.Thread(target=_connect_worker, daemon=True)
    thread.start()
    return {"status": "connecting", "error": None}
