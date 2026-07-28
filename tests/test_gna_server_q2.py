"""tests/test_gna_server_q2.py — POST /api/workbook-q2 (Wave 4 two-file
upload). Mirrors tests/test_cli_run_q2.py's fixture builders and
tests/test_gna_server.py's TestClient pattern.

The route flattens the two raw uploads (G&A multi-tab + flat A&T) into the
one canonical single-sheet workbook, validates THAT flattened file, and
points server_state at it. These tests run the flatten step for real against
tiny openpyxl fixtures (no network, $0) and assert the whole handoff:
flatten happened, the flat file validates clean, and the tracked workbook is
the flat file -- never a raw upload.

config.Q2_FLAT_XLSX and config.WORKSPACE_DIR are monkeypatched into tmp so
nothing touches the real workspace/, and the process-global server_state is
snapshotted + restored so this file never leaks tracked state into the other
server tests (e.g. test_gna_server.py::test_run_without_workbook_is_400).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from gna_pipeline import config, flatten_q2, ingest
from gna_server.state import state as server_state

# Same tab shapes as test_cli_run_q2.py: a normal G&A tab (Amount in col E,
# header on row 6) and a canonical flat A&T sheet.
HEADER_NORMAL = [
    "Quarter", "Period", "Date", "Counterparty", "Amount",
    "Invoice Link", "Page", "Type", "USERID", "DESCRPN", "ADDLDESC",
]
AT_HEADER = flatten_q2.OUTPUT_HEADERS[:-1]


def _save(tmp_path, name, tabs):
    path = tmp_path / name
    wb = Workbook()
    for i, (title, rows) in enumerate(tabs):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = title
        for r in rows:
            ws.append(r)
    wb.save(path)
    return path


def _ga_bytes(tmp_path):
    rows = [
        ["Salary & Wages"],           # A1
        ["MR70000000"],               # A2
        [], [], [],                   # rows 3-5 filler -> header on row 6
        HEADER_NORMAL,
        ["2026Q2", "202604", "2026-04-15", "Acme Corp", 1500, "http://inv/1", "1", "Invoice", "jdoe", "Rent", "x"],
    ]
    return _save(tmp_path, "ga.xlsx", [("Rent", rows)]).read_bytes()


def _at_bytes(tmp_path):
    ma_row = ["202604", "R99", "AT", "9", config.MA_ACCTNUM, "", 1000, "Legal fee", "", "2026-04-20",
              "GBP", "acq legal", "", "csmith", "Legal Fees", "2026Q2", "", "http://inv/at1", 0.79, 790]
    return _save(tmp_path, "at.xlsx", [(flatten_q2._AT_SHEET_NAME, [AT_HEADER, ma_row])]).read_bytes()


@pytest.fixture
def client():
    from gna_server.app import app
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    """Redirect the flat-output + workspace paths into tmp AND snapshot/restore
    the process-global server_state, so this file is fully hermetic."""
    monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(config, "Q2_FLAT_XLSX", tmp_path / "q2_flat.xlsx")

    before = (
        server_state.workbook_path,
        list(server_state.workbook_checks),
        server_state.workbook_row_count,
    )
    yield tmp_path
    path, checks, rows = before
    with server_state._lock:  # restore exactly what the other tests expect
        server_state.workbook_path = path
        server_state.workbook_checks = checks
        server_state.workbook_row_count = rows


def test_two_file_upload_flattens_validates_and_tracks_flat_file(client, isolated_workspace, tmp_path):
    resp = client.post(
        "/api/workbook-q2",
        files={
            "ga": ("q2G&Av1.xlsx", _ga_bytes(tmp_path), "application/octet-stream"),
            "at": ("q2A&T.xlsx", _at_bytes(tmp_path), "application/octet-stream"),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The flattened file was written and validates clean (all checks ok).
    assert config.Q2_FLAT_XLSX.is_file()
    assert [c["ok"] for c in body["checks"]] == [True, True, True]
    assert body["row_count"] == 2  # 1 G&A + 1 A&T (MA) row
    assert body["has_existing_classifications"] is False
    assert body["name"] == "q2_flat.xlsx"
    assert body["flatten"]["at_rows"] == 1
    assert body["flatten"]["total_rows"] == 2
    assert body["flatten"]["ga_tabs_included"] == 1

    # server_state now tracks the FLAT file (not a raw upload), so /api/quarters
    # + /api/run read the flattened workbook.
    assert server_state.workbook_path == config.Q2_FLAT_XLSX

    # The flat workbook is genuinely pipeline-clean (zero column warnings).
    packets, stats = ingest.read_packets(str(config.Q2_FLAT_XLSX), sheet=config.SHEET_NAME)
    assert stats["column_warnings"] == []
    assert len(packets) == 2

    # Raw uploads are transient -- the private subfolder is gone.
    assert not (config.WORKSPACE_DIR / "q2_raw").exists()


def test_rejects_bad_ga_extension(client, isolated_workspace, tmp_path):
    resp = client.post(
        "/api/workbook-q2",
        files={
            "ga": ("notes.txt", b"hello", "text/plain"),
            "at": ("q2A&T.xlsx", _at_bytes(tmp_path), "application/octet-stream"),
        },
    )
    assert resp.status_code == 400
    assert "G&A" in resp.json()["detail"]


def test_rejects_bad_at_extension(client, isolated_workspace, tmp_path):
    resp = client.post(
        "/api/workbook-q2",
        files={
            "ga": ("q2G&Av1.xlsx", _ga_bytes(tmp_path), "application/octet-stream"),
            "at": ("deals.csv", b"a,b,c", "text/csv"),
        },
    )
    assert resp.status_code == 400
    assert "A&T" in resp.json()["detail"]
