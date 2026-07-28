"""Tests for `--n` split sampling and `--rows` targeted mode in cli.cmd_run.

Sample-run semantics (--n, a cheap end-to-end rehearsal of both phases):
  - N//2 M&A rows (acctnum config.MA_ACCTNUM) feed the deal-profile sweep ONLY
    and are never classified;
  - the remaining N - N//2 rows come from the non-M&A pool and flow to
    Phase 2 classification;
  - Phase 0 preps the WHOLE sample, M&A included, so sweep rows get their
    invoices resolved;
  - if fewer M&A rows exist than N//2, the shortfall goes to the
    classification sample.
Full runs (no --n): every in-scope non-M&A row is classified; every in-scope
M&A row feeds the sweep and never reaches the classifier (auto non_recurring
by rule).
Row mode (--rows): classifies exactly the named rows with no sweep at all and
bypasses the --months/--min-usd scope filters.

All tests run with dry_run=True and stubbed stage functions: no API client is
ever constructed, nothing is written to disk, zero cost.
"""

from __future__ import annotations

import argparse

import pytest

from gna_pipeline import (
    classify,
    config,
    deal_profile,
    ingest,
    invoice_mining,
    prep,
)
from gna_pipeline import cli


def _packet(row_idx: int, *, acctnum: str = "GA10000000", period: str = "202501") -> dict:
    """Minimal RowPacket-shaped dict; only the fields the sampling path reads."""
    return {
        "row_idx": row_idx,
        "acctnum": acctnum,
        "period": period,
        "amount": 100.0,
        "amount_was_blank": False,
        "invoice_url": None,
        "userid": "someuser",
    }


def _work_item(packet: dict) -> dict:
    """Minimal WorkItem-shaped dict for the partition/forecast path."""
    return {
        "packet": packet,
        "row_hash": f"hash{packet['row_idx']}",
        "flags": [],
        "had_invoice": "no",
        "invoice_accessed": "no",
    }


def _sweep_forecast_stub(rows: int) -> dict:
    return {
        "rows": rows,
        "est_batches": 0,
        "input_tokens_low": 0.0,
        "input_tokens_high": 0.0,
        "cost_low_usd": 0.0,
        "cost_high_usd": 0.0,
        "max_workers": 1,
        "wall_clock_est_min": 0.0,
    }


def _classify_forecast_stub(rows: int) -> dict:
    return {
        "rows": rows,
        "rows_with_invoice": 0,
        "est_batches": 0,
        "input_tokens_low": 0.0,
        "input_tokens_high": 0.0,
        "output_tokens_est": 0.0,
        "cost_low_usd": 0.0,
        "cost_high_usd": 0.0,
        "max_workers": 1,
        "wall_clock_est_min": 0.0,
    }


ORDINARY_COUNT = 6
MA_COUNT = 3


@pytest.fixture
def sampling_env(monkeypatch):
    """Wire cmd_run against synthetic packets, recording what each stage sees.

    Packets: 6 ordinary rows (idx 1-6), then 3 M&A rows (idx 101-103) at the
    END of the file, so a naive first-N slice would never reach them.
    """
    packets = [_packet(i) for i in range(1, ORDINARY_COUNT + 1)]
    packets += [
        _packet(100 + i, acctnum=config.MA_ACCTNUM) for i in range(1, MA_COUNT + 1)
    ]
    stats = {"column_warnings": [], "total_data_rows": len(packets)}

    recorder: dict = {}

    monkeypatch.setattr(ingest, "read_packets", lambda *a, **k: (packets, stats))
    monkeypatch.setattr(invoice_mining, "load_lookup_index", lambda *a, **k: {})
    monkeypatch.setattr(config, "load_rate_limits", lambda: None)

    def fake_prepare_rows(pkts, *a, **k):
        recorder["phase0_packets"] = list(pkts)
        s = prep._empty_stats()
        s["work_items"] = [_work_item(p) for p in pkts]
        return s

    monkeypatch.setattr(prep, "prepare_rows", fake_prepare_rows)

    def fake_sweep_forecast(items, model, rate_limits):
        recorder["sweep_items"] = list(items)
        return _sweep_forecast_stub(len(items))

    monkeypatch.setattr(deal_profile, "sweep_forecast", fake_sweep_forecast)

    def fake_forecast(work_items, model, rate_limits):
        recorder["forecast_work_items"] = list(work_items)
        return _classify_forecast_stub(len(work_items))

    monkeypatch.setattr(classify, "forecast", fake_forecast)

    recorder["all_packets"] = packets
    return recorder


def _run_args(**overrides) -> argparse.Namespace:
    base = dict(
        workbook="dummy.xlsx",
        n=None,
        rows=None,
        quarters=None,
        months="all",   # scope-neutral: the synthetic packets are the test's scope
        min_usd=0.0,
        dry_run=True,
        yes=True,
        no_fetch=True,
        model=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _acctnums(packets: list[dict]) -> set[str]:
    return {p["acctnum"] for p in packets}


def _sweep_row_idxs(recorder: dict) -> list[int]:
    return [it["packet"]["row_idx"] for it in recorder["sweep_items"]]


def test_sample_split_half_ma_half_classification(sampling_env, capsys):
    """--n 5 -> 2 M&A rows (5//2, no rounding up) sweep-only, 3 classified."""
    rc = cli.cmd_run(_run_args(n=5))
    assert rc == 0

    # The sweep forecast sees exactly the first 2 M&A rows.
    assert _sweep_row_idxs(sampling_env) == [101, 102]

    # Phase 0 preps the whole sample (sweep rows need invoices too);
    # classification sees only the 3 non-M&A rows.
    assert len(sampling_env["phase0_packets"]) == 5
    forecast_packets = [it["packet"] for it in sampling_env["forecast_work_items"]]
    assert len(forecast_packets) == 3
    assert config.MA_ACCTNUM not in _acctnums(forecast_packets)

    out = capsys.readouterr().out
    assert "sample --n 5" in out
    assert "feed the deal-profile sweep" in out


def test_sample_n1_gives_zero_ma(sampling_env):
    """--n 1 -> 1//2 = 0 M&A rows, 1 classification row."""
    rc = cli.cmd_run(_run_args(n=1))
    assert rc == 0
    assert sampling_env["sweep_items"] == []
    assert len(sampling_env["phase0_packets"]) == 1
    assert config.MA_ACCTNUM not in _acctnums(sampling_env["phase0_packets"])


def test_sample_ma_shortfall_goes_to_classification(sampling_env, capsys):
    """--n 10 wants 5 M&A but only 3 exist -> 3 swept, 7 requested for
    classification (capped at the 6 ordinary rows available)."""
    rc = cli.cmd_run(_run_args(n=10))
    assert rc == 0
    assert _sweep_row_idxs(sampling_env) == [101, 102, 103]
    assert len(sampling_env["phase0_packets"]) == MA_COUNT + ORDINARY_COUNT
    assert len(sampling_env["forecast_work_items"]) == ORDINARY_COUNT
    assert "remainder given to the classification sample" in capsys.readouterr().out


def test_no_n_full_run_partitions_sweep_from_classification(sampling_env):
    """Without --n, Phase 0 preps every in-scope row, the sweep sees the full
    M&A set, and classification sees everything EXCEPT the M&A rows (they are
    auto non_recurring by rule and never reach the classifier)."""
    rc = cli.cmd_run(_run_args(n=None))
    assert rc == 0

    full = len(sampling_env["all_packets"])
    assert len(sampling_env["phase0_packets"]) == full
    assert config.MA_ACCTNUM in _acctnums(sampling_env["phase0_packets"])

    assert len(sampling_env["sweep_items"]) == MA_COUNT

    forecast_packets = [it["packet"] for it in sampling_env["forecast_work_items"]]
    assert len(forecast_packets) == ORDINARY_COUNT
    assert config.MA_ACCTNUM not in _acctnums(forecast_packets)


# ---------------------------------------------------------------------------
# --rows targeted-test mode
# ---------------------------------------------------------------------------

def test_rows_selects_only_named_rows(sampling_env, capsys):
    """--rows 3,5 -> exactly those two packets classified, no sweep rows."""
    rc = cli.cmd_run(_run_args(rows="3,5"))
    assert rc == 0
    assert [p["row_idx"] for p in sampling_env["phase0_packets"]] == [3, 5]
    assert sampling_env["sweep_items"] == []
    assert "--rows selected 2 row(s)" in capsys.readouterr().out


def test_rows_still_classifies_a_row_already_in_results_jsonl(sampling_env, monkeypatch, tmp_path):
    """There is no resume mechanism any more (Wave 1 removed it entirely): a
    row already durable in results.jsonl from a prior run is NOT skipped --
    --rows 3 still sends row 3 through Phase 0 exactly as if no prior record
    existed."""
    from gna_pipeline import persistence
    from gna_pipeline.contract import make_decision_record, row_hash

    results_path = tmp_path / "results.jsonl"
    monkeypatch.setattr(config, "RESULTS_JSONL", results_path)

    target = sampling_env["all_packets"][2]  # row_idx 3
    prior = make_decision_record(
        packet=target, row_hash=row_hash(target), phase="classify",
        classification="recurring", basis="row_text_routine",
        reasoning="prior run's decision", evidence="[row] prior evidence",
    )
    persistence.append_record(results_path, prior)

    rc = cli.cmd_run(_run_args(rows="3"))
    assert rc == 0
    assert [p["row_idx"] for p in sampling_env["phase0_packets"]] == [3]


def test_rows_unknown_row_errors(sampling_env, capsys):
    """--rows pointing at a nonexistent row is a clean error, not a silent no-op."""
    rc = cli.cmd_run(_run_args(rows="999"))
    assert rc == 2
    assert "not found" in capsys.readouterr().out


def test_rows_and_n_are_mutually_exclusive(sampling_env):
    rc = cli.cmd_run(_run_args(rows="3", n=5))
    assert rc == 2
