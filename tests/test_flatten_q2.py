"""tests/test_flatten_q2.py — flatten_q2 pre-processes the two Q2 workbooks
(multi-tab G&A + flat A&T) into one flat .xlsx in ingest.py's canonical
format.

Fixtures are authored with openpyxl (pyxlsb cannot write), building
multi-sheet workbooks row-by-row so each test controls A1/A2 and puts the
header on an arbitrary row -- see `_save`.
"""

from __future__ import annotations

from openpyxl import Workbook

from gna_pipeline import config, flatten_q2, ingest

# A header row matching the G&A tab's expected columns (Amount in col E,
# 0-based idx 4) -- the "normal" layout.
HEADER_NORMAL = [
    "Quarter", "Period", "Date", "Counterparty", "Amount",
    "Invoice Link", "Page", "Type", "USERID", "DESCRPN", "ADDLDESC",
]

# A&T's own header row: the 20 canonical columns (everything but Source Tab),
# already in the flat format the A&T workbook ships as.
AT_HEADER = flatten_q2.OUTPUT_HEADERS[:-1]


def _save(tmp_path, name, tabs):
    """tabs: list of (title, rows) where each `rows` entry is a full literal
    row (list) -- callers control exactly which Excel row holds A1/A2/header/
    data, unlike test_ingest_sheet.py's single-header-row `_save`."""
    path = tmp_path / name
    wb = Workbook()
    for i, (title, rows) in enumerate(tabs):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = title
        for r in rows:
            ws.append(r)
    wb.save(path)
    return str(path)


def _empty_at(tmp_path, name="at_empty.xlsx"):
    """An A&T workbook with no 'USD Amount' column -- flatten_q2 must skip it
    gracefully (0 rows, a header_detect_warning) rather than crash. Used by
    tests that only care about G&A-tab behavior."""
    return _save(tmp_path, name, [("Sheet1", [["Foo", "Bar"], [1, 2]])])


def _at_workbook(tmp_path, data_rows, name="at.xlsx", sheet_title=flatten_q2._AT_SHEET_NAME):
    """data_rows: list of A&T data rows (list of lists) -- the real A&T sheet
    mixes M&A-account rows in among other-account rows and thousands of
    fully-blank trailing rows, so tests build the sheet as a list, not a
    single row."""
    return _save(tmp_path, name, [(sheet_title, [AT_HEADER, *data_rows])])


def _empty_ga(tmp_path, name="ga_empty.xlsx"):
    """A G&A workbook with only a non-data (Template) tab -- every G&A row in
    round-trip tests that only care about A&T output comes from this."""
    return _save(tmp_path, name, [("Template", [["Template"], ["n/a"]])])


# ---------------------------------------------------------------------------
# 1. Normal G&A tab
# ---------------------------------------------------------------------------

def test_normal_ga_tab(tmp_path):
    rows = [
        ["Salary & Wages"],           # A1
        ["MR70000000"],               # A2
        [], [], [],                   # rows 3-5: filler, pushes header to row 6
        HEADER_NORMAL,                # row 6
        ["2026Q2", "202604", "2026-04-15", "Acme Corp", 1500, "http://inv/1", "1", "Invoice", "jdoe", "Monthly rent", "see attached"],
    ]
    gna_path = _save(tmp_path, "ga.xlsx", [("Rent", rows)])
    at_path = _empty_at(tmp_path)

    headers, out_rows, stats = flatten_q2.flatten(gna_path, at_path)

    assert len(out_rows) == 1
    row = out_rows[0]

    def col(name):
        return row[headers.index(name)]

    assert col("PERIOD") == "202604"
    assert col("ACCTNUM") == "MR70000000"
    assert col("Category") == "Salary & Wages"
    assert col("Source Tab") == "Salary & Wages"
    assert col("OCURRCODE") == "USD"
    assert col("Exchange Rate") == 1
    assert col("AMT") == 1500
    assert col("USD Amount") == 1500
    assert col("DESCRPN") == "Monthly rent"
    assert col("ADDLDESC") == "see attached"
    assert col("USERID") == "jdoe"
    assert col("Quarter") == "2026Q2"
    assert col("Image URL - Hyperlink") == "http://inv/1"

    assert stats["tabs_included"] == [
        {"name": "Rent", "mri": "MR70000000", "rows_written": 1, "blank_period_dropped": 0}
    ]
    assert stats["tabs_skipped"] == []


# ---------------------------------------------------------------------------
# 2. T&E-style tab: Amount in col F (col E a spacer); blank-period children
#    dropped, only the period-bearing parent survives.
# ---------------------------------------------------------------------------

def test_te_style_tab_drops_blank_period_children(tmp_path):
    header_te = [
        "Quarter", "Period", "Date", "Counterparty", "Spacer", "Amount",
        "Invoice Link", "Page", "Type", "USERID", "DESCRPN", "ADDLDESC",
    ]
    rows = [
        ["Travel & Entertainment"],
        ["MR71000000"],
        [],
        header_te,
        ["2026Q2", "202605", "2026-05-01", "Delta Air", "", 900, "", "", "Airfare", "jdoe", "Trip to NYC", ""],
        ["", "", "2026-05-01", "Delta Air", "", 500, "", "", "Airfare", "jdoe", "  sub-leg 1", ""],
        ["", "", "2026-05-02", "Delta Air", "", 400, "", "", "Airfare", "jdoe", "  sub-leg 2", ""],
    ]
    gna_path = _save(tmp_path, "te.xlsx", [("T&E", rows)])
    at_path = _empty_at(tmp_path)

    headers, out_rows, stats = flatten_q2.flatten(gna_path, at_path)

    assert len(out_rows) == 1
    row = out_rows[0]
    assert row[headers.index("PERIOD")] == "202605"
    assert row[headers.index("AMT")] == 900
    assert row[headers.index("USD Amount")] == 900

    tab_stats = stats["tabs_included"][0]
    assert tab_stats["rows_written"] == 1
    assert tab_stats["blank_period_dropped"] == 2


# ---------------------------------------------------------------------------
# 3. Audit-Fees-style tab: header on row 5, found by scan (not the fallback).
# ---------------------------------------------------------------------------

def test_header_on_row_5_detected_by_scan(tmp_path):
    # Header unambiguously on row 5 (0-based idx 4) -- not the row-6 fallback.
    rows = [
        ["Audit Fees"],            # row 1 = A1
        ["MR72000000"],            # row 2 = A2
        [],                        # row 3 filler
        [],                        # row 4 filler
        HEADER_NORMAL,             # row 5 = header
        ["2026Q2", "202606", "2026-06-10", "BigFour LLP", 12000, "", "", "", "cfo", "Annual audit fee", ""],
    ]
    gna_path = _save(tmp_path, "audit.xlsx", [("Audit Fees", rows)])
    at_path = _empty_at(tmp_path)

    headers, out_rows, stats = flatten_q2.flatten(gna_path, at_path)

    assert len(out_rows) == 1
    assert out_rows[0][headers.index("AMT")] == 12000
    assert stats["tabs_included"] == [
        {"name": "Audit Fees", "mri": "MR72000000", "rows_written": 1, "blank_period_dropped": 0}
    ]
    # Found by scan, not the row-6 fallback -- no warning naming this tab.
    # (header_detect_warnings also carries the _empty_at() A&T stub's "no USD
    # Amount column" notice, which is unrelated to G&A header detection.)
    assert not any("Audit Fees" in w for w in stats["header_detect_warnings"])


# ---------------------------------------------------------------------------
# 4. Non-data tab: A2 not MR... -> skipped.
# ---------------------------------------------------------------------------

def test_non_mri_tab_is_skipped(tmp_path):
    rows = [
        ["Template"],
        ["n/a"],
        [],
        HEADER_NORMAL,
    ]
    gna_path = _save(tmp_path, "template.xlsx", [("Template", rows)])
    at_path = _empty_at(tmp_path)

    headers, out_rows, stats = flatten_q2.flatten(gna_path, at_path)

    assert out_rows == []
    assert len(stats["tabs_skipped"]) == 1
    assert stats["tabs_skipped"][0]["name"] == "Template"
    assert stats["tabs_included"] == []


# ---------------------------------------------------------------------------
# 5. A&T-format sheet: real file mixes M&A-account rows in among other-account
#    rows and thousands of fully-blank trailing rows. Only ACCTNUM ==
#    config.MA_ACCTNUM rows are emitted; passthrough is 1:1 on those,
#    currency fields preserved.
# ---------------------------------------------------------------------------

def test_at_sheet_filters_to_ma_acctnum_only(tmp_path):
    ma_row_gbp = ["202604", "R99", "AT", "9", config.MA_ACCTNUM, "", 1000, "Legal fee", "", "2026-04-20",
                  "GBP", "acquisition legal costs", "", "csmith", "Legal Fees", "2026Q2", "", "http://inv/at1",
                  0.79, 790]
    ma_row_usd = ["202605", "R101", "AT", "9", config.MA_ACCTNUM, "", 2000, "Advisory fee", "", "2026-05-05",
                  "USD", "advisory services", "", "csmith", "Advisory", "2026Q2", "", "",
                  1, 2000]
    other_acct_row = ["202604", "R100", "AT", "9", "MR99999999", "", 500, "Unrelated fee", "", "2026-04-21",
                       "USD", "unrelated", "", "csmith", "Other Category", "2026Q2", "", "",
                       1, 500]
    blank_row = [None] * len(AT_HEADER)

    at_path = _at_workbook(tmp_path, [ma_row_gbp, other_acct_row, blank_row, ma_row_usd])
    gna_path = _empty_ga(tmp_path)

    headers, out_rows, stats = flatten_q2.flatten(gna_path, at_path)

    # Only the two M&A-account rows survive -- the other-account row and the
    # fully-blank row are both excluded.
    assert len(out_rows) == 2
    assert stats["at_rows"] == 2

    def col(row, name):
        return row[headers.index(name)]

    gbp_out = next(r for r in out_rows if col(r, "OCURRCODE") == "GBP")
    assert col(gbp_out, "Source Tab") == "A&T"
    assert col(gbp_out, "Exchange Rate") == 0.79
    assert col(gbp_out, "AMT") == 1000
    assert col(gbp_out, "USD Amount") == 790
    assert col(gbp_out, "Category") == "Legal Fees"

    usd_out = next(r for r in out_rows if col(r, "OCURRCODE") == "USD")
    assert col(usd_out, "Source Tab") == "A&T"
    assert col(usd_out, "Exchange Rate") == 1
    assert col(usd_out, "AMT") == col(usd_out, "USD Amount") == 2000

    assert all(col(r, "ACCTNUM") == config.MA_ACCTNUM for r in out_rows)


# ---------------------------------------------------------------------------
# 6. Round-trip: flattener output feeds ingest.read_packets cleanly.
# ---------------------------------------------------------------------------

def test_round_trip_through_ingest_zero_column_warnings(tmp_path):
    ga_rows = [
        ["Salary & Wages"],
        ["MR70000000"],
        [], [], [],
        HEADER_NORMAL,
        ["2026Q2", "202604", "2026-04-15", "Acme Corp", 1500, "http://inv/1", "1", "Invoice", "jdoe", "Monthly rent", "see attached"],
        ["2026Q2", "202605", "2026-05-15", "Acme Corp", 1550, "http://inv/2", "1", "Invoice", "jdoe", "Monthly rent", "see attached"],
    ]
    gna_path = _save(tmp_path, "ga.xlsx", [("Rent", ga_rows)])

    at_row = ["202604", "R99", "AT", "9", config.MA_ACCTNUM, "", 1000, "Legal fee", "", "2026-04-20",
              "GBP", "acquisition legal costs", "", "csmith", "Legal Fees", "2026Q2", "", "http://inv/at1",
              0.79, 790]
    at_path = _at_workbook(tmp_path, [at_row])

    headers, rows, stats = flatten_q2.flatten(gna_path, at_path)
    assert stats["total_rows"] == len(rows) == 3

    out_path = str(tmp_path / "flat_out.xlsx")
    flatten_q2.write_flat_workbook(headers, rows, out_path)

    packets, read_stats = ingest.read_packets(out_path)
    assert read_stats["column_warnings"] == []
    assert len(packets) == stats["total_rows"]
