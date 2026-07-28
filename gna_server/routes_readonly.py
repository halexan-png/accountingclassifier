"""routes_readonly.py — GET /api/state, /api/quarters, /api/results,
/api/download/{key}, /api/docs/{key} (v2 UI handoff §7.1).

Disjoint from routes_upload.py and routes_settings.py: every handler here
only reads (gna_pipeline.config paths, ingest.read_packets/deal_profile
helpers, gna_server.state, run_manager.manager) — it writes nothing.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from gna_pipeline import config, deal_profile, ingest

from .run_manager import manager
from .state import state as server_state

router = APIRouter()

UI_VERSION = "0.1.0"

_DOWNLOAD_PATHS = {
    # classified.xlsx is the ONLY thing the operator ever downloads -- it
    # already carries summary.json's tally (Run Summary sheet) and
    # quarter_deal_profile.json's contents (Deal Profile sheet) as tabs, so
    # those two aren't offered as separate download artifacts.
    "classified": config.CLASSIFIED_XLSX,
}

_DOC_PATHS = {
    "quickstart": config.REPO_ROOT / "QUICKSTART.md",
    "how_it_works": config.REPO_ROOT / "HOW_IT_WORKS.md",
}

# Cached by the tracked workbook's mtime so repeated polling (the Configure
# modal's quarter picker) doesn't re-read the whole workbook every time.
_quarters_cache: dict[str, Any] = {"mtime": None, "payload": None}


@router.get("/api/ping")
def ping() -> dict:
    """Liveness probe for the frontend's idle-timeout detector (app.js).

    Deliberately trivial AND deliberately exempt from the activity 'touch' in
    app.py's middleware (see _LIVENESS_PATHS there): polling it must NOT reset
    the idle countdown, or an open browser tab could keep the server alive
    forever and auto-shutdown would never fire. Once the watchdog stops the
    server (gna_server.lifecycle), this stops answering; the frontend sees the
    failed pings and shows its 'session timed out -- please relaunch' screen
    instead of silently failing the operator's next click."""
    return {"ok": True}


@router.get("/api/activity")
def activity() -> dict:
    """Keep-alive beacon. The frontend (app.js) calls this on a throttled
    cadence ONLY while the operator is actually interacting -- moving the
    mouse, typing in Additional Context, clicking tabs, scrolling as they read.
    Unlike /api/ping it is NOT in app.py's _LIVENESS_PATHS, so it DOES reset the
    idle-shutdown clock (lifecycle.touch() runs in the middleware). That's the
    whole point: genuine on-screen activity must prevent a timeout, while a
    walked-away tab -- which sends no beacons -- still idles out."""
    return {"ok": True}


@router.get("/api/state")
def get_state() -> dict:
    dir_ready = config.INVOICE_DIR.is_dir() and any(config.INVOICE_DIR.glob("*.pdf"))
    csv_ready = config.INVOICE_LOOKUP_CSV.is_file()
    return {
        "workbook": server_state.workbook_snapshot(),
        "api_key_present": config.api_key_present(),
        "invoice_library": {"dir_ready": dir_ready, "csv_ready": csv_ready},
        "run": manager.run_snapshot(),
        "model_default": config.DEFAULT_MODEL,
        # Two-tier model policy (config.model_for_batch): model_default is the
        # FLOOR every non-invoice row runs on; invoice_model is what any batch
        # holding a readable invoice upgrades to. Purely additive -- existing
        # consumers of model_default are unaffected.
        "floor_model": config.DEFAULT_MODEL,
        "invoice_model": config.INVOICE_MODEL,
        "ui_version": UI_VERSION,
    }


@router.get("/api/quarters")
def get_quarters() -> dict:
    path = server_state.workbook_path
    if path is None:
        raise HTTPException(400, "no workbook uploaded yet (POST /api/workbook first)")

    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise HTTPException(404, f"tracked workbook is missing on disk: {path}") from exc

    if _quarters_cache["mtime"] == mtime and _quarters_cache["payload"] is not None:
        return _quarters_cache["payload"]

    try:
        packets, stats = ingest.read_packets(str(path), sheet=config.SHEET_NAME)
    except Exception as exc:  # noqa: BLE001 — a wrong/incompatible workbook (e.g. the
        # expected worksheet is absent) must surface as a clean 400, never a 500. The
        # frontend already gates on POST /api/workbook's checks so it won't normally
        # call this for a bad file, but a direct call must not crash the server.
        raise HTTPException(400, f"could not read this workbook (wrong file or missing worksheet?): {exc}") from exc
    ma_packets = deal_profile.select_ma_packets(packets)
    available = deal_profile.quarters_available(packets)

    quarters = []
    for label in available:
        rows = sum(1 for p in packets if deal_profile.quarter_of(p.get("period") or "") == label)
        ma_rows = sum(1 for p in ma_packets if deal_profile.quarter_of(p.get("period") or "") == label)
        quarters.append({
            "label": label,
            "rows": rows,
            "ma_rows": ma_rows,
        })

    payload = {"quarters": quarters, "warnings": stats["column_warnings"]}
    _quarters_cache["mtime"] = mtime
    _quarters_cache["payload"] = payload
    return payload


@router.get("/api/results")
def get_results() -> dict:
    summary = None
    if config.SUMMARY_JSON.is_file():
        try:
            summary = json.loads(config.SUMMARY_JSON.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            summary = None

    artifacts = []
    for key, path in _DOWNLOAD_PATHS.items():
        if path.is_file():
            st = path.stat()
            artifacts.append({
                "key": key, "name": path.name, "path": str(path),
                "bytes": st.st_size, "mtime": st.st_mtime,
            })

    return {"summary": summary, "artifacts": artifacts}


@router.get("/api/download/{key}")
def download(key: str) -> FileResponse:
    path = _DOWNLOAD_PATHS.get(key)
    if path is None:
        raise HTTPException(404, f"unknown download key {key!r}")
    if not path.is_file():
        raise HTTPException(404, f"{path.name} does not exist yet")
    return FileResponse(path, filename=path.name)


@router.get("/api/docs/{key}")
def get_doc(key: str) -> dict:
    path = _DOC_PATHS.get(key)
    if path is None:
        raise HTTPException(404, f"unknown doc key {key!r}")
    if not path.is_file():
        raise HTTPException(404, f"{path.name} not found")
    return {"markdown": path.read_text(encoding="utf-8")}
