"""tests/test_cli_run_q2.py — cli.cmd_run_q2 is a thin two-file wrapper: it
flattens the A&T + G&A workbooks into one flat sheet, then delegates to
cmd_run verbatim against that flat workbook.

These tests stub cli.cmd_run (recording the Namespace it receives) so the
paid pipeline never runs and nothing hits the network -- exactly the pattern
in test_cli_sampling.py. The flatten step itself runs for real against tiny
openpyxl fixtures, so the flat workbook is actually produced and its handoff
verified.
"""

from __future__ import annotations

import argparse

from openpyxl import Workbook

from gna_pipeline import cli, config, flatten_q2, ingest

# G&A tab header (normal layout, Amount in col E) and A&T canonical header.
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
    return str(path)


def _ga_workbook(tmp_path):
    rows = [
        ["Salary & Wages"],           # A1
        ["MR70000000"],               # A2
        [], [], [],                   # rows 3-5 filler -> header on row 6
        HEADER_NORMAL,
        ["2026Q2", "202604", "2026-04-15", "Acme Corp", 1500, "http://inv/1", "1", "Invoice", "jdoe", "Rent", "x"],
    ]
    return _save(tmp_path, "ga.xlsx", [("Rent", rows)])


def _at_workbook(tmp_path):
    ma_row = ["202604", "R99", "AT", "9", config.MA_ACCTNUM, "", 1000, "Legal fee", "", "2026-04-20",
              "GBP", "acq legal", "", "csmith", "Legal Fees", "2026Q2", "", "http://inv/at1", 0.79, 790]
    return _save(tmp_path, "at.xlsx", [(flatten_q2._AT_SHEET_NAME, [AT_HEADER, ma_row])])


def _args(tmp_path, **overrides):
    base = dict(
        ga=_ga_workbook(tmp_path),
        at=_at_workbook(tmp_path),
        output=str(tmp_path / "flat.xlsx"),
        workbook=None,
        n=None, rows=None,
        quarters=None, months=None, quarter=None,
        guided=False,
        min_usd=None, dry_run=True, yes=False, no_fetch=False,
        model=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _stub_cmd_run(monkeypatch):
    captured = {}

    def fake(a):
        captured["args"] = a
        return 0

    monkeypatch.setattr(cli, "cmd_run", fake)
    return captured


def test_flattens_and_delegates_to_run(tmp_path, monkeypatch):
    captured = _stub_cmd_run(monkeypatch)
    args = _args(tmp_path)

    rc = cli.cmd_run_q2(args)
    assert rc == 0

    # Flat workbook actually written, and it is pipeline-clean (zero warnings).
    out = tmp_path / "flat.xlsx"
    assert out.is_file()
    packets, stats = ingest.read_packets(str(out))
    assert stats["column_warnings"] == []
    assert len(packets) == 2  # 1 G&A + 1 A&T (MA) row

    # Delegated to cmd_run pointed at the flat workbook.
    passed = captured["args"]
    assert str(passed.workbook) == str(out)


def test_defaults_to_guided_when_no_scope_given(tmp_path, monkeypatch):
    captured = _stub_cmd_run(monkeypatch)
    cli.cmd_run_q2(_args(tmp_path))
    assert captured["args"].guided is True


def test_explicit_quarter_disables_guided_default(tmp_path, monkeypatch):
    captured = _stub_cmd_run(monkeypatch)
    cli.cmd_run_q2(_args(tmp_path, quarter="2026Q2"))
    # An explicit scope must NOT be overridden with a guided pick.
    assert captured["args"].guided is False
    assert captured["args"].quarter == "2026Q2"


def test_sample_flag_disables_guided_default(tmp_path, monkeypatch):
    captured = _stub_cmd_run(monkeypatch)
    cli.cmd_run_q2(_args(tmp_path, n=5))
    assert captured["args"].guided is False
    assert captured["args"].n == 5


def test_default_output_path_used_when_output_none(tmp_path, monkeypatch):
    # Redirect the default flat-output path into tmp so the test never writes
    # to the real workspace/.
    default_flat = tmp_path / "default_flat.xlsx"
    monkeypatch.setattr(config, "Q2_FLAT_XLSX", default_flat)
    captured = _stub_cmd_run(monkeypatch)
    cli.cmd_run_q2(_args(tmp_path, output=None))
    assert str(captured["args"].workbook) == str(default_flat)
    assert default_flat.is_file()


def test_missing_ga_file_is_clean_error(tmp_path, monkeypatch):
    _stub_cmd_run(monkeypatch)
    rc = cli.cmd_run_q2(_args(tmp_path, ga=str(tmp_path / "nope.xlsx")))
    assert rc == 2


def test_missing_at_file_is_clean_error(tmp_path, monkeypatch):
    _stub_cmd_run(monkeypatch)
    rc = cli.cmd_run_q2(_args(tmp_path, at=str(tmp_path / "nope.xlsx")))
    assert rc == 2
