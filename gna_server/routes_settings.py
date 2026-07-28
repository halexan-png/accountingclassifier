"""routes_settings.py — GET/PUT /api/settings/{key} (v2 UI handoff §6.5/§7.1).

Disjoint from routes_upload.py and routes_readonly.py: writes are confined
to exactly the three known doctrine paths below, nothing else. Edits are
PERMANENT (a plain path.write_text to the real file the pipeline already
reads live every run) -- this is the only disk-write path for doctrine
content; Additional Context on Launch stays session-only and never lands
here (v2 handoff §4.3/§4.10).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from gna_pipeline import config

from .run_manager import manager

router = APIRouter()

_PATHS = {
    "classifier": config.CLASSIFIER_DOCTRINE_MD,
    "dealbuilder": config.DEALBUILDER_MD,
    "companynorm": config.COMPANY_NORMS_MD,
}


class SettingsPut(BaseModel):
    content: str


@router.get("/api/settings/{key}")
def get_settings(key: str) -> dict:
    path = _PATHS.get(key)
    if path is None:
        raise HTTPException(404, f"unknown settings key {key!r}")
    content = path.read_text(encoding="utf-8") if path.is_file() else ""
    return {"content": content, "path": str(path)}


@router.put("/api/settings/{key}")
def put_settings(key: str, body: SettingsPut) -> dict:
    path = _PATHS.get(key)
    if path is None:
        raise HTTPException(404, f"unknown settings key {key!r}")
    if manager.is_active():
        raise HTTPException(409, "a run is active; try again once it finishes")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.content, encoding="utf-8")
    return {"content": body.content, "path": str(path)}
