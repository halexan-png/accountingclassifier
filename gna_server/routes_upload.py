"""routes_upload.py — POST /api/workbook, /api/invoices, /api/deal-profile/upload
(v2 UI handoff §6.1/§6.3/§6.4, §7.1).

Disjoint from routes_readonly.py and routes_settings.py (this wave's
fan-out group per ORCHESTRATION_RULES.md rule 3): every write here touches
only workspace/, externalinvoices/, or data/input/dealprofile/, and reads
only through run_manager.manager.is_active() / gna_server.state — no
shared mutable object with the other two route files beyond that.
"""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from fastapi import APIRouter, HTTPException, UploadFile

from gna_pipeline import config, flatten_q2, ingest, pipeline

from .run_manager import manager
from .state import state as server_state

router = APIRouter()

_WORKBOOK_SUFFIXES = (".xlsb", ".xlsx")

# Upload size caps (server-side hygiene — bound disk-fill / memory on the
# routes that lacked a limit; invoices already cap via config.INVOICE_MAX_BYTES).
# Workbooks are uncapped (operator's call — real xlsb exports can run large);
# profile JSON stays capped since it's always tiny.
_DEAL_PROFILE_MAX_BYTES = 25 * 1024 * 1024
_CONTEXT_MAX_BYTES = 25 * 1024 * 1024


def _safe_filename(name: str) -> str:
    """Strip any directory component -- the only thing standing between an
    upload's filename and an accidental (or hostile) path escape."""
    cleaned = Path(name).name
    if not cleaned or cleaned in (".", ".."):
        raise HTTPException(400, f"invalid filename {name!r}")
    return cleaned


def _reject_while_running() -> None:
    if manager.is_active():
        raise HTTPException(409, "a run is active; try again once it finishes")


@router.post("/api/workbook")
async def upload_workbook(file: UploadFile) -> dict:
    _reject_while_running()

    filename = _safe_filename(file.filename or "")
    suffix = Path(filename).suffix.lower()
    if suffix not in _WORKBOOK_SUFFIXES:
        raise HTTPException(400, f"unsupported workbook type {suffix!r}; expected .xlsb or .xlsx")

    content = await file.read()
    config.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    target = config.WORKSPACE_DIR / filename

    # Single-file rule: remove whatever this server previously tracked (if
    # it was a different file) so workspace/ doesn't accumulate uploads --
    # never touches a file the operator placed by hand that was never
    # tracked here.
    previous = server_state.workbook_path
    if previous is not None and previous != target and previous.exists():
        previous.unlink()

    target.write_bytes(content)

    result = ingest.validate_workbook(str(target), sheet=config.SHEET_NAME)
    server_state.set_workbook(
        target,
        checks=result["checks"],
        row_count=result["row_count"],
    )

    return {
        "name": filename,
        "checks": result["checks"],
        "row_count": result["row_count"],
        "has_existing_classifications": result["has_existing_classifications"],
    }


async def _read_workbook_upload(file: UploadFile, label: str) -> tuple[str, bytes]:
    """Validate ONE workbook upload (suffix only -- size is uncapped) and read its bytes.

    Shared by the two-file Q2 route below for its `ga`/`at` inputs -- same
    suffix rule as the single-file /api/workbook route, but `label`
    ("G&A"/"A&T") is folded into the error so the operator knows WHICH
    dropzone was wrong."""
    filename = _safe_filename(file.filename or "")
    suffix = Path(filename).suffix.lower()
    if suffix not in _WORKBOOK_SUFFIXES:
        raise HTTPException(400, f"unsupported {label} workbook type {suffix!r}; expected .xlsb or .xlsx")
    return filename, await file.read()


@router.post("/api/workbook-q2")
async def upload_workbook_q2(ga: UploadFile, at: UploadFile) -> dict:
    """Q2 two-file upload (v2 handoff §W4): take the multi-tab G&A workbook
    (`ga`) and the flat A&T workbook (`at`), FLATTEN them into the one
    canonical single-sheet workbook the rest of the server already consumes
    (config.Q2_FLAT_XLSX), then validate + track THAT flattened file exactly
    as /api/workbook tracks its single upload.

    Downstream (/api/quarters, /api/run, /api/results, recover) is unchanged:
    it all reads the one tracked workbook_path, which is now the flat file.
    The flatten step is pure/$0 and already unit-tested (flatten_q2). The two
    raw uploads are transient inputs -- written to a private subfolder, read
    by flatten_q2, then deleted, so workspace/ ends up holding only the flat
    workbook (keeps config.discover_workbook's exactly-one rule intact)."""
    _reject_while_running()

    ga_name, ga_bytes = await _read_workbook_upload(ga, "G&A")
    at_name, at_bytes = await _read_workbook_upload(at, "A&T")

    config.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    raw_dir = config.WORKSPACE_DIR / "q2_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    # Prefix so a G&A and A&T file that happen to share a name can't collide,
    # while preserving the suffix flatten_q2 dispatches on (.xlsb vs .xlsx).
    ga_path = raw_dir / f"ga__{ga_name}"
    at_path = raw_dir / f"at__{at_name}"

    try:
        ga_path.write_bytes(ga_bytes)
        at_path.write_bytes(at_bytes)
        headers, rows, flat_stats = flatten_q2.flatten(str(ga_path), str(at_path))
        flatten_q2.write_flat_workbook(headers, rows, str(config.Q2_FLAT_XLSX))
    finally:
        # Raw uploads are transient -- never leave them in workspace/ (they would
        # trip discover_workbook's exactly-one rule and waste disk).
        shutil.rmtree(raw_dir, ignore_errors=True)

    # Single-file rule (mirrors /api/workbook): drop whatever this server
    # previously tracked, if it was a DIFFERENT file, so workspace/ doesn't
    # accumulate (e.g. switching from a single-file upload to the Q2 flow).
    previous = server_state.workbook_path
    if previous is not None and previous != config.Q2_FLAT_XLSX and previous.exists():
        previous.unlink()

    # Validate the FLATTENED file, never the raw uploads (the raw G&A/A&T
    # workbooks are NOT the canonical single-sheet format and would fail).
    result = ingest.validate_workbook(str(config.Q2_FLAT_XLSX), sheet=config.SHEET_NAME)
    server_state.set_workbook(
        config.Q2_FLAT_XLSX,
        checks=result["checks"],
        row_count=result["row_count"],
    )

    return {
        "name": config.Q2_FLAT_XLSX.name,
        "ga_name": ga_name,
        "at_name": at_name,
        "checks": result["checks"],
        "row_count": result["row_count"],
        "has_existing_classifications": result["has_existing_classifications"],
        "flatten": {
            "ga_tabs_included": len(flat_stats["tabs_included"]),
            "ga_tabs_skipped": len(flat_stats["tabs_skipped"]),
            "at_rows": flat_stats["at_rows"],
            "total_rows": flat_stats["total_rows"],
            "header_warnings": flat_stats["header_detect_warnings"],
        },
    }


@router.post("/api/invoices")
async def upload_invoices(files: list[UploadFile]) -> dict:
    _reject_while_running()

    config.INVOICE_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    rejected: list[dict] = []

    for file in files:
        try:
            filename = _safe_filename(file.filename or "")
        except HTTPException as exc:
            rejected.append({"name": file.filename or "", "reason": str(exc.detail)})
            continue

        if Path(filename).suffix.lower() != ".pdf":
            rejected.append({"name": filename, "reason": "not a .pdf file"})
            continue

        content = await file.read()
        if len(content) > config.INVOICE_MAX_BYTES:
            rejected.append({"name": filename, "reason": f"exceeds {config.INVOICE_MAX_BYTES} bytes"})
            continue

        target = config.INVOICE_DIR / filename
        if target.resolve().parent != config.INVOICE_DIR.resolve():
            rejected.append({"name": filename, "reason": "path escapes externalinvoices/"})
            continue

        target.write_bytes(content)
        saved.append(filename)

    return {"saved": saved, "rejected": rejected}


@router.post("/api/deal-profile/upload")
async def upload_deal_profile(profile: UploadFile, context_txt: UploadFile | None = None) -> dict:
    _reject_while_running()

    if profile.size is not None and profile.size > _DEAL_PROFILE_MAX_BYTES:
        raise HTTPException(413, f"deal profile exceeds the {_DEAL_PROFILE_MAX_BYTES}-byte limit")

    raw = await profile.read()
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, f"not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or "entries" not in parsed:
        raise HTTPException(400, "not a recognizable quarter_deal_profile.json (missing 'entries')")

    pipeline.write_json(config.DEAL_PROFILE_JSON, parsed)

    if context_txt is not None:
        if context_txt.size is not None and context_txt.size > _DEAL_PROFILE_MAX_BYTES:
            raise HTTPException(413, f"context file exceeds the {_DEAL_PROFILE_MAX_BYTES}-byte limit")
        context_raw = await context_txt.read()
        config.DEAL_PROFILE_CONTEXT_TXT.parent.mkdir(parents=True, exist_ok=True)
        config.DEAL_PROFILE_CONTEXT_TXT.write_text(context_raw.decode("utf-8"), encoding="utf-8")

    quarters = parsed.get("quarters", []) or []

    return {"quarters": quarters, "entry_count": len(parsed.get("entries", []) or [])}


# --- Additional-context extraction (UI "Additional Context" file drop) ---------
# The operator may drop a .txt/.md/.docx file whose TEXT is folded into the
# session-only User Deal Context. Nothing is written to disk here: the bytes are
# read into memory, the extracted text is returned to the browser, and the app
# stores nothing -- identical in effect to typing into the textarea (v2 handoff
# §4.3). .docx is parsed with the Python standard library only (a .docx is just
# a zip of XML), so this adds NO new dependency.
_DOCX_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_CONTEXT_SUFFIXES = (".txt", ".md", ".docx")


def _docx_to_text(data: bytes) -> str:
    """Extract paragraph text from a .docx's word/document.xml (stdlib only)."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml = zf.read("word/document.xml")
    root = ET.fromstring(xml)
    paragraphs: list[str] = []
    for para in root.iter(f"{_DOCX_NS}p"):
        runs = [node.text for node in para.iter(f"{_DOCX_NS}t") if node.text]
        paragraphs.append("".join(runs))
    return "\n".join(paragraphs).strip()


@router.post("/api/context/extract")
async def extract_context(file: UploadFile) -> dict:
    """Return the plain text of an uploaded .txt/.md/.docx for the Additional
    Context field. Reads in memory and writes NOTHING to disk (session-only)."""
    filename = _safe_filename(file.filename or "")
    suffix = Path(filename).suffix.lower()
    if suffix not in _CONTEXT_SUFFIXES:
        raise HTTPException(400, f"unsupported context file {suffix!r}; use .txt, .md, or .docx")

    data = await file.read()
    if len(data) > _CONTEXT_MAX_BYTES:
        raise HTTPException(413, f"context file exceeds the {_CONTEXT_MAX_BYTES}-byte limit")

    if suffix == ".docx":
        try:
            text = _docx_to_text(data)
        except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
            raise HTTPException(400, f"could not read .docx: {exc}") from exc
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1", errors="replace")

    return {"name": filename, "text": text}
