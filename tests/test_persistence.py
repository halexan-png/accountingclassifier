"""tests/test_persistence.py — durability round-trip for results.jsonl.

Replaces tests/test_persistence_resume.py (Wave 1 removed
`persistence.load_resume_state` entirely, along with `contract.packet_key` —
there is no resume/skip mechanism anywhere in the pipeline any more; every
run re-decides every in-scope row). What's left in persistence.py is pure
durability: `append_record` writes one DecisionRecord per JSONL line
(fsync'd), and `load_all_records` reads the whole history back, tolerating a
malformed/truncated trailing line and a missing file. These are the only two
functions left to pin.
"""

from __future__ import annotations

from gna_pipeline import persistence
from gna_pipeline.contract import (
    RowPacket,
    make_decision_record,
    make_error_record,
    row_hash,
)


def _packet(row_idx: int, ref: str) -> RowPacket:
    """A minimal but complete RowPacket; `ref` just distinguishes rows."""
    return RowPacket(
        row_idx=row_idx,
        period="2026-06",
        acctnum="60000",
        acctname="Professional Fees",
        ref=ref,
        entityid="ENT1",
        propertyname=None,
        amount=1234.56,
        amount_was_blank=False,
        currency="USD",
        descrptn="Advisory services",
        adddesc=None,
        source="AP",
        entrdate="2026-06-15",
        userid="jsmith",
        invoice_url=None,
        vendid="V1",
        invoice_no="INV-1",
        invcdate="2026-06-10",
        vendor="Acme LLP",
    )


def test_load_all_records_missing_file_returns_empty_list(tmp_path):
    assert persistence.load_all_records(tmp_path / "does_not_exist.jsonl") == []


def test_append_and_load_all_records_round_trip_preserves_every_record_in_order(tmp_path):
    """A good decision and a subsequent error record for a DIFFERENT row are
    both durable and both come back from load_all_records, in append order —
    there is no filtering of error records the way the old resume index used
    to do; that distinction is now purely a consumer-side concern (e.g.
    cli.cmd_recover folding to the latest record per row)."""
    good_packet = _packet(1, ref="GOOD")
    error_packet = _packet(2, ref="ERR")

    good = make_decision_record(
        packet=good_packet,
        row_hash=row_hash(good_packet),
        phase="classify",
        classification="recurring",
        basis="row_text_routine",
        reasoning="routine recurring vendor",
        evidence="[row] Advisory services",
    )
    err = make_error_record(
        packet=error_packet,
        row_hash=row_hash(error_packet),
        phase="classify",
        error_msg="API call failed after retries: 400 bad request",
    )

    path = tmp_path / "results.jsonl"
    persistence.append_record(path, good)
    persistence.append_record(path, err)

    records = persistence.load_all_records(path)
    assert len(records) == 2
    assert records[0]["packet"]["ref"] == "GOOD"
    assert records[0]["error"] is None
    assert records[1]["packet"]["ref"] == "ERR"
    assert records[1]["error"] is not None


def test_append_record_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "results.jsonl"
    packet = _packet(1, ref="R1")
    record = make_decision_record(
        packet=packet, row_hash=row_hash(packet), phase="classify",
        classification="recurring", basis="row_text_routine",
        reasoning="routine", evidence="[row] Advisory services",
    )
    persistence.append_record(path, record)
    assert path.is_file()
    assert len(persistence.load_all_records(path)) == 1


def test_load_all_records_skips_malformed_trailing_line(tmp_path):
    """A truncated/malformed last line (as a crash mid-write would leave) is
    logged and skipped, not raised -- earlier, complete lines still load."""
    packet = _packet(1, ref="R1")
    record = make_decision_record(
        packet=packet, row_hash=row_hash(packet), phase="classify",
        classification="recurring", basis="row_text_routine",
        reasoning="routine", evidence="[row] Advisory services",
    )
    path = tmp_path / "results.jsonl"
    persistence.append_record(path, record)
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"packet": {"row_idx": 2}, "truncated": tr\n')  # not valid JSON

    records = persistence.load_all_records(path)
    assert len(records) == 1
    assert records[0]["packet"]["ref"] == "R1"


def test_a_later_record_for_the_same_row_is_not_deduplicated_by_persistence(tmp_path):
    """load_all_records returns EVERY line, even multiple records for the same
    row (e.g. an error retried and then re-decided on a later run) -- folding
    to the latest is a consumer's job (cli.cmd_recover), not persistence's."""
    packet = _packet(1, ref="RETRY")
    err = make_error_record(
        packet=packet, row_hash=row_hash(packet), phase="classify",
        error_msg="API call failed after retries: 400 bad request",
    )
    good = make_decision_record(
        packet=packet, row_hash=row_hash(packet), phase="classify",
        classification="non_recurring", basis="invoice_content",
        reasoning="one-time deal cost", evidence="[invoice p.1] closing statement",
    )

    path = tmp_path / "results.jsonl"
    persistence.append_record(path, err)
    persistence.append_record(path, good)

    records = persistence.load_all_records(path)
    assert len(records) == 2
    assert records[0]["error"] is not None
    assert records[1]["error"] is None
    assert records[1]["decision"]["classification"] == "non_recurring"
