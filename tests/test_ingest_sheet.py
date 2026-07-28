"""tests/test_ingest_sheet.py — the data sheet is chosen BY COLUMNS, not by the
tab's name.

A workbook whose columns match is accepted even if its sheet is called
"Sheet1"; a workbook with no matching columns on any sheet is rejected; and in
a multi-sheet workbook the sheet carrying the columns wins regardless of its
name (with config.SHEET_NAME only breaking ties).
"""

from __future__ import annotations

from openpyxl import Workbook

from gna_pipeline import config, ingest

# A real header row: matches the COLUMN_SPEC aliases and resolves with zero
# column warnings (so "Worksheet structure" passes).
DATA_HEADERS = [
    "PERIOD", "REF", "SOURCE", "ENTITYID", "ACCTNUM", "DEPARTMENT", "AMT", "DESCRPN",
    "PDENTRY", "ENTRDATE", "OCURRCODE", "ADDLDESC", "LASTDATE", "USERID", "Category",
    "Quarter", "Image URL - Hyperlink", "Exchange Rate", "USD Amount",
]
DATA_ROW = [
    "202601", "R1", "S", "1", "5500", "D", "", "an expense", "", "2026-01-15", "USD",
    "", "", "u", "C", "2026Q1", "", "1", "1500",
]


def _save(tmp_path, name, sheets):
    """sheets: list of (title, header_row, data_rows); the first is the active sheet."""
    path = tmp_path / name
    wb = Workbook()
    for i, (title, header, rows) in enumerate(sheets):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = title
        ws.append(header)
        for r in rows:
            ws.append(r)
    wb.save(path)
    return str(path)


def _accepted(res):
    return all(c["ok"] for c in res["checks"])


def test_accepts_matching_columns_on_any_tab_name(tmp_path):
    # Tab named "Sheet1" (NOT config.SHEET_NAME), but the columns match.
    path = _save(tmp_path, "temp.xlsx", [("Sheet1", DATA_HEADERS, [DATA_ROW, DATA_ROW])])
    res = ingest.validate_workbook(path, sheet=config.SHEET_NAME)
    assert _accepted(res)
    assert res["row_count"] == 2


def test_rejects_when_no_sheet_has_the_columns(tmp_path):
    path = _save(tmp_path, "wrong.xlsx", [("Sheet1", ["Foo", "Bar", "Baz"], [[1, 2, 3]])])
    res = ingest.validate_workbook(path, sheet=config.SHEET_NAME)
    assert not _accepted(res)
    structure = next(c for c in res["checks"] if c["label"] == "Worksheet structure")
    assert structure["ok"] is False


def test_multi_sheet_picks_the_data_sheet_by_columns(tmp_path):
    # A decoy first sheet + the data on an oddly-named later tab.
    path = _save(tmp_path, "multi.xlsx", [
        ("Summary", ["Totals", "x"], [["grand", 999]]),
        ("q1-export", DATA_HEADERS, [DATA_ROW]),
    ])
    assert ingest._resolve_data_sheet(path, config.SHEET_NAME) == "q1-export"
    assert _accepted(ingest.validate_workbook(path, sheet=config.SHEET_NAME))


def test_named_sheet_breaks_ties_when_present(tmp_path):
    # Two column-complete sheets; the one named config.SHEET_NAME wins the tie.
    path = _save(tmp_path, "named.xlsx", [
        ("Other", DATA_HEADERS, [DATA_ROW]),
        (config.SHEET_NAME, DATA_HEADERS, [DATA_ROW]),
    ])
    assert ingest._resolve_data_sheet(path, config.SHEET_NAME) == config.SHEET_NAME
