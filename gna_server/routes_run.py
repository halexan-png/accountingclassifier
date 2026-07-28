"""routes_run.py — POST /api/run, GET /api/run/events, POST /api/run/confirm,
POST /api/run/cancel (v2 UI handoff §7.1/§7.2).

Built together with run_manager.py (not fanned out to a separate wave
group): every handler here does nothing but validate a request shape and
call straight through to `manager`, so it is the same "hub" as the
run-manager core, not a disjoint file another agent could safely build in
parallel.

Absolute rule (v2 handoff §8, ORCHESTRATION_RULES.md rule 5): `yes` is never
accepted from the request body and is always passed as False; the pipeline's
own console.confirm() gate (bridged through `manager`) is the only thing
that can advance a paid run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from gna_pipeline import cli

from .run_manager import RunConflict, manager
from .state import state as server_state

router = APIRouter()


class RunRequest(BaseModel):
    kind: Literal["run", "deal-profile", "recover"]
    quarter: str | None = None
    deal_profile_quarters: int | None = None
    min_usd: float | None = None
    user_deal_context: str | None = None


class ConfirmRequest(BaseModel):
    confirm_id: str
    answer: bool


class CancelResponse(BaseModel):
    ok: bool


def _require_workbook() -> Path:
    if server_state.workbook_path is None:
        raise HTTPException(400, "no workbook uploaded yet (POST /api/workbook first)")
    return server_state.workbook_path


def _build_run_kwargs(req: RunRequest, workbook: Path) -> dict:
    if req.quarter is None:
        raise HTTPException(400, "quarter is required for kind=run")
    try:
        quarters_arg, months_arg = cli._quarter_scope(req.quarter)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return dict(
        workbook=workbook,
        model=None,
        n=None,
        rows=None,
        quarters=quarters_arg,
        months=months_arg,
        min_usd=req.min_usd,
        dry_run=False,
        yes=False,
        no_fetch=False,
        user_deal_context_override=req.user_deal_context,
        # UI runs never block mid-run on the "continue with a partial deal
        # profile?" confirm: the process screen has no surface to answer it, so
        # it would hang until the 10-minute confirm timeout. The up-front money
        # gate already authorized spend; a partial profile is still usable.
        continue_on_partial_profile=True,
    )


def _build_deal_profile_kwargs(req: RunRequest, workbook: Path) -> dict:
    if req.quarter is not None:
        quarters_arg = req.quarter
    elif req.deal_profile_quarters is not None:
        quarters_arg = str(req.deal_profile_quarters)
    else:
        quarters_arg = None  # deal_profile.parse_quarters_arg default: latest available quarter
    return dict(
        workbook=workbook, quarters=quarters_arg, months=None,
        min_usd=req.min_usd, no_fetch=False, model=None,
    )


def _build_recover_kwargs(req: RunRequest, workbook: Path) -> dict:
    months_arg = None
    if req.quarter is not None:
        try:
            _quarters_arg, months_arg = cli._quarter_scope(req.quarter)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return dict(workbook=workbook, months=months_arg, min_usd=req.min_usd)


@router.post("/api/run")
def start_run(req: RunRequest) -> dict:
    workbook = _require_workbook()

    if req.kind == "run":
        kwargs = _build_run_kwargs(req, workbook)
    elif req.kind == "deal-profile":
        kwargs = _build_deal_profile_kwargs(req, workbook)
    else:
        kwargs = _build_recover_kwargs(req, workbook)

    try:
        run_id = manager.start_run(req.kind, kwargs)
    except RunConflict as exc:
        raise HTTPException(409, str(exc)) from exc

    return {"run_id": run_id}


def _json_safe(obj: object) -> str:
    """json.dumps fallback for any value that isn't natively serializable
    (e.g. raw PDF `bytes` from an invoice read). A single non-serializable
    value in ONE progress event must never 500 the whole SSE stream -- that
    would make the browser reconnect and hit the same poisoned event forever.
    Bytes collapse to a short placeholder; anything else to its repr."""
    if isinstance(obj, (bytes, bytearray)):
        return f"<{len(obj)} bytes>"
    return repr(obj)


@router.get("/api/run/events")
def run_events(after: int = 0) -> StreamingResponse:
    def gen():
        for entry in manager.stream_events(after):
            yield f"data: {json.dumps(entry, default=_json_safe)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/api/run/confirm")
def answer_confirm(req: ConfirmRequest) -> dict:
    ok = manager.answer_confirm(req.confirm_id, req.answer)
    if not ok:
        raise HTTPException(404, f"unknown or already-answered confirm_id {req.confirm_id!r}")
    return {"ok": True}


@router.post("/api/run/cancel")
def cancel_run() -> CancelResponse:
    ok = manager.cancel()
    return CancelResponse(ok=ok)
