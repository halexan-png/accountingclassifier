"""tests/test_invariants.py — characterization tests for the load-bearing
accounting-correctness rules.

These pin behavior that must survive any refactor verbatim:

  1. The unreadable-invoice-with-named-deal downgrade in classify._coerce_row
     is an accounting rule, not error handling: a named-deal claim that could
     not be checked against a readable invoice (fetch attempted and failed)
     forces human_review and records the override. (Whether row text and
     invoice content actually AGREE on a deal is now doctrine-level, decided
     by the model within Q1 itself — not a separate code-enforced field; see
     doctrines/classifier.md "A claimed tie is not a shown tie".)
  2. The basis guard: a "deal_profile" basis with no recognized deal is a
     model inconsistency — downgraded to basis "none" + basis_mismatch flag.
  3. Never drop a row: an invalid classification becomes an error record
     (human_review + non-null error), never a raise.
  4. The audit fingerprint (contract.row_hash) is pinned to a golden value so
     an accidental hash-input change cannot slip through silently, and it
     reacts to the invoice actually read. NOTE: there is no resume/skip
     mechanism anywhere in the pipeline any more (Wave 1 removed
     `contract.packet_key` and `persistence.load_resume_state` entirely) —
     row_hash is a pure audit fingerprint, never consulted to skip a row.
  5. Invoice-token mining decides which rows claim invoice evidence — the
     golden extraction table in token_extraction_cases.txt must keep passing.
  6. The classify_rows tool schema orders evidence and reasoning BEFORE
     classification (and classification before missing_info) — this is the
     order the model actually fills the JSON in, so a conclusion-first,
     reasoning-backfilled decision (the former bug: classification sat right
     after row_idx, evidence was second-to-last) can never silently return.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gna_pipeline import classify, config, invoice_mining, invoice_read, persistence, prep, prompts, scheduling
from gna_pipeline.contract import (
    make_decision_record,
    row_hash,
)

CASES_FILE = Path(__file__).with_name("token_extraction_cases.txt")


def _packet(**overrides) -> dict:
    base = dict(
        row_idx=7,
        period="202601",
        acctnum="60000",
        acctname="Professional Fees",
        ref="J-000123",
        entityid="ENT1",
        propertyname=None,
        amount=1234.56,
        amount_was_blank=False,
        currency="USD",
        descrptn="Advisory services",
        adddesc=None,
        source="AP",
        entrdate="2026-01-15",
        userid="jsmith",
        invoice_url=None,
        vendid="V1",
        invoice_no="INV-1",
        invcdate="2026-01-10",
        vendor="Acme LLP",
    )
    base.update(overrides)
    return base


def _item(**overrides) -> dict:
    packet = overrides.pop("packet", _packet())
    base = dict(
        packet=packet,
        row_hash=row_hash(packet),
        flags=[],
        had_invoice="no",
        invoice_accessed="no",
        invoice=None,
    )
    base.update(overrides)
    return base


def _model_row(**overrides) -> dict:
    base = dict(
        classification="recurring",
        basis="row_text_routine",
        reasoning="routine vendor",
        evidence="[row] Advisory services",
        recognized_deal="none",
        missing_info=None,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1-3: classify._coerce_row
# ---------------------------------------------------------------------------

def test_unreadable_invoice_with_named_deal_forces_human_review():
    # invoice_unavailable means a fetch was ATTEMPTED and FAILED (not "never
    # referenced") — the invoice was never actually read, so a named-deal claim
    # resting on row text alone must not pass as a clean classification.
    record = classify._coerce_row(
        _item(flags=["invoice_unavailable"]),
        _model_row(classification="non_recurring", basis="deal_profile",
                   recognized_deal="Project Alpha"),
        model="test-model",
    )
    decision = record["decision"]
    assert decision["classification"] == "human_review"
    assert decision["override"]["from"] == "non_recurring"
    assert decision["override"]["reason"] == classify._UNVERIFIED_DEAL_OVERRIDE_REASON
    assert decision["missing_info"]


def test_unreadable_invoice_without_named_deal_is_unaffected():
    record = classify._coerce_row(
        _item(flags=["invoice_unavailable"]),
        _model_row(classification="recurring", recognized_deal="none"),
        model="test-model",
    )
    decision = record["decision"]
    assert decision["classification"] == "recurring"
    assert "override" not in decision


def test_named_deal_without_unavailable_invoice_is_unaffected():
    record = classify._coerce_row(
        _item(flags=[]),
        _model_row(classification="non_recurring", basis="deal_profile",
                   recognized_deal="Project Alpha"),
        model="test-model",
    )
    decision = record["decision"]
    assert decision["classification"] == "non_recurring"
    assert "override" not in decision


def test_deal_profile_basis_without_recognized_deal_downgrades():
    record = classify._coerce_row(
        _item(),
        _model_row(basis="deal_profile", recognized_deal="none"),
        model="test-model",
    )
    decision = record["decision"]
    assert decision["basis"] == "none"
    assert "basis_mismatch" in record["flags"]


def test_recognized_deal_adds_deal_profile_match_flag():
    record = classify._coerce_row(
        _item(),
        _model_row(classification="non_recurring", basis="deal_profile",
                   recognized_deal="Project Alpha"),
        model="test-model",
    )
    assert "deal_profile_match" in record["flags"]
    assert record["decision"]["basis"] == "deal_profile"


def test_human_review_without_missing_info_gets_backfill():
    record = classify._coerce_row(
        _item(),
        _model_row(classification="human_review", missing_info=None),
        model="test-model",
    )
    assert record["decision"]["missing_info"] == "model omitted missing_info"


def test_invalid_classification_becomes_error_record_never_a_raise():
    record = classify._coerce_row(
        _item(),
        _model_row(classification="totally_bogus"),
        model="test-model",
    )
    assert record["error"] is not None
    assert record["decision"]["classification"] == "human_review"


def test_malformed_enums_coerce_to_safe_defaults():
    record = classify._coerce_row(
        _item(),
        _model_row(basis="junk_basis"),
        model="test-model",
    )
    decision = record["decision"]
    assert decision["classification"] == "recurring"  # untouched
    assert decision["basis"] == "none"
    assert record["error"] is None


# ---------------------------------------------------------------------------
# 1b: invoice_read cross-check, invoice_date capture, truncation flag, and the
#     failed-invoice notice / trailing reminder (Track A additions).
# ---------------------------------------------------------------------------

def _text_invoice(**overrides) -> dict:
    base = dict(
        kind="text",
        source="url",
        path_or_url="http://example/inv.pdf",
        text="RE: Project Titan  Invoice date: 2026-01-10",
        pages_read="1-2",
        page_count=2,
        read_path="text",
        error=None,
    )
    base.update(overrides)
    return base


def test_invoice_read_mismatch_when_document_sent_but_model_denies():
    # A pdf/text document WAS sent, yet the model claims none was attached.
    record = classify._coerce_row(
        _item(invoice=_text_invoice()),
        _model_row(invoice_read="none_attached"),
        model="test-model",
    )
    assert "invoice_read_mismatch" in record["flags"]


def test_invoice_read_fabrication_when_no_document_but_model_claims_read():
    # No document sent, yet the model claims it read one — a fabrication signal.
    record = classify._coerce_row(
        _item(invoice=None),
        _model_row(invoice_read="read"),
        model="test-model",
    )
    assert "invoice_read_mismatch" in record["flags"]


def test_invoice_read_match_sets_field_and_no_flag():
    record = classify._coerce_row(
        _item(invoice=_text_invoice()),
        _model_row(invoice_read="read"),
        model="test-model",
    )
    assert "invoice_read_mismatch" not in record["flags"]
    assert record["decision"]["invoice_read"] == "read"


def test_invoice_date_captured_and_blank_becomes_null():
    got = classify._coerce_row(
        _item(invoice=_text_invoice()),
        _model_row(invoice_read="read", invoice_date="2026-01-10"),
        model="test-model",
    )
    assert got["decision"]["invoice_date"] == "2026-01-10"

    blank = classify._coerce_row(
        _item(invoice=_text_invoice()),
        _model_row(invoice_read="read", invoice_date="   "),
        model="test-model",
    )
    assert blank["decision"]["invoice_date"] is None


def test_invoice_truncated_flag_set_only_on_short_read():
    """Truncated is a GAP in pages_read (an edge-only read that skipped the
    middle), not merely falling short of page_count: a CSV-hinted sub-range
    read in FULL (e.g. "3-8" of a 50-page file) is NOT truncated even though
    pages_read != page_count -- only an edge-only read (first N + last N,
    middle skipped) leaves the gap that flags this."""
    truncated = classify._coerce_row(
        _item(invoice=_text_invoice(pages_read="1-2,10-12", page_count=12)),
        _model_row(invoice_read="read"),
        model="test-model",
    )
    assert "invoice_truncated" in truncated["flags"]

    full = classify._coerce_row(
        _item(invoice=_text_invoice(pages_read="1-8", page_count=8)),
        _model_row(invoice_read="read"),
        model="test-model",
    )
    assert "invoice_truncated" not in full["flags"]

    # The fixed former bug: a fully-read CSV sub-range short of page_count
    # used to be falsely flagged truncated; a contiguous range has no gap.
    csv_subrange = classify._coerce_row(
        _item(invoice=_text_invoice(pages_read="3-8", page_count=50)),
        _model_row(invoice_read="read"),
        model="test-model",
    )
    assert "invoice_truncated" not in csv_subrange["flags"]


def test_was_truncated_helper():
    # Gap-based: an edge-only read (first + last pages, middle skipped) IS truncated...
    assert invoice_read.was_truncated({"pages_read": "1-2,24-25", "page_count": 25}) is True
    # ...but a contiguous read is NOT, even short of page_count -- this is the
    # fixed former bug (a fully-read CSV sub-range like "1-8" of a 12-page
    # file used to be falsely flagged truncated).
    assert invoice_read.was_truncated({"pages_read": "1-8", "page_count": 12}) is False
    assert invoice_read.was_truncated({"pages_read": "1-8", "page_count": 8}) is False
    assert invoice_read.was_truncated({"pages_read": None, "page_count": 12}) is False
    assert invoice_read.was_truncated({}) is False
    assert invoice_read.was_truncated(None) is False


def test_select_pages_full_window_reads_every_page():
    """A window at or under config.PAGE_FULL_READ_MAX reads every page --
    no edge-only truncation."""
    assert invoice_read._select_pages(12, None) == list(range(1, 13))
    assert invoice_read._select_pages(config.PAGE_FULL_READ_MAX, None) == list(
        range(1, config.PAGE_FULL_READ_MAX + 1)
    )


def test_select_pages_long_window_reads_edges_only():
    """A window longer than config.PAGE_FULL_READ_MAX reads only the first +
    last config.PAGE_EDGE_COUNT pages -- the middle is skipped."""
    assert invoice_read._select_pages(25, None) == [1, 2, 24, 25]


def test_select_pages_csv_hint_window_respected():
    # A hinted range whose span fits within PAGE_FULL_READ_MAX reads it whole.
    assert invoice_read._select_pages(50, "10-15") == list(range(10, 16))
    # A hinted range whose OWN span exceeds PAGE_FULL_READ_MAX reads only its
    # own edges -- the window is the hint, not the whole document.
    assert invoice_read._select_pages(100, "1-30") == [1, 2, 29, 30]


def test_select_pages_unparsable_or_out_of_range_hint_falls_back_to_whole_document():
    assert invoice_read._select_pages(5, "not-a-range") == [1, 2, 3, 4, 5]
    # p_start (10) is beyond page_count (5) -- the hint drops through to the
    # whole-document window instead.
    assert invoice_read._select_pages(5, "10-20") == [1, 2, 3, 4, 5]


def test_format_pages_renders_singletons_and_ranges():
    assert invoice_read._format_pages([1, 2, 3]) == "1-3"
    assert invoice_read._format_pages([1, 2, 24, 25]) == "1-2,24-25"
    # A lone page still renders "N-N", not bare "N" -- every downstream
    # parser treats each comma-separated part as an "A-B" range.
    assert invoice_read._format_pages([5]) == "5-5"
    assert invoice_read._format_pages([]) == ""


def test_parse_pages_read_sums_multi_range():
    assert scheduling._parse_pages_read("1-2,24-25") == 4
    assert scheduling._parse_pages_read("1-8") == 8
    assert scheduling._parse_pages_read(None) is None
    assert scheduling._parse_pages_read("not-a-range") is None


def test_failed_invoice_emits_not_retrieved_notice():
    items = [_item(invoice={"kind": "error", "error": "login page detected"})]
    content = prompts.build_batch_user_content(items)
    joined = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
    assert "could NOT be retrieved" in joined
    assert "login page detected" in joined


def test_batch_user_content_trailing_reminder_is_last():
    content = prompts.build_batch_user_content([_item()])
    assert content[-1]["type"] == "text"
    assert content[-1]["text"] == prompts.TRAILING_REMINDER


def test_classify_rows_tool_orders_evidence_and_reasoning_before_classification():
    """Pins the classify_rows tool's field order — both the properties dict
    (the order Claude fills the JSON in) and the required list — so evidence/
    reasoning can never again sit after classification/missing_info."""
    row_schema = prompts.CLASSIFY_ROWS_TOOL["input_schema"]["properties"]["rows"]["items"]
    for order in (list(row_schema["properties"]), row_schema["required"]):
        assert order.index("evidence") < order.index("classification")
        assert order.index("reasoning") < order.index("classification")
        assert order.index("classification") < order.index("missing_info")


# ---------------------------------------------------------------------------
# 4: the audit fingerprint (contract.row_hash) — NOT a resume key. There is no
# resume mechanism left anywhere in the pipeline (Wave 1 deleted
# contract.packet_key and persistence.load_resume_state entirely); every run
# re-decides every in-scope row. row_hash is still pinned to a golden value
# because it is the persisted audit fingerprint on every DecisionRecord —
# an accidental hash-input change would silently look like a different run's
# output when diffing history.
# ---------------------------------------------------------------------------

# Golden value for the exact packet built by _packet() with no overrides.
GOLDEN_ROW_HASH = "79108050b67126b863686d6782441496b356b5fc98b5af03b9bc1f777885317b"


def test_row_hash_matches_golden_value():
    assert row_hash(_packet()) == GOLDEN_ROW_HASH


def test_row_hash_changes_with_invoice_sha256():
    """row_hash bakes in the invoice actually read — a different invoice
    (or none at all) must produce a different fingerprint."""
    packet = _packet()
    assert row_hash(packet, invoice_sha256="a" * 64) != row_hash(packet)


# ---------------------------------------------------------------------------
# Adjacent: the reclass Phase-0 rule — highest precedence, never human review
# ---------------------------------------------------------------------------

def _run_phase0(packet: dict) -> tuple[list[dict], prep.Phase0Stats]:
    """Run Phase 0 over a single packet with no lookup state and no
    network (fetch_urls=False), collecting every emitted record."""
    emitted: list[dict] = []
    stats = prep.prepare_rows(
        [packet], lookup_index={},
        emit=emitted.append, fetch_urls=False,
    )
    return emitted, stats


@pytest.mark.parametrize(
    "field,text",
    [
        ("descrptn", "Reclass to prior period"),
        ("descrptn", "RECLASS ADJUSTMENT"),
        ("descrptn", "reclassification of expense"),  # substring, not whole word
        ("adddesc", "Q1 reclass entry"),
    ],
)
def test_reclass_text_auto_labels_reclass_in_phase0(field, text):
    """A DESCRPN or ADDLDESC containing 'reclass' (case-insensitive substring)
    is resolved in Phase 0 as `reclass` / basis `reclass_rule`, flagged, and
    NEVER handed to the classifier (absent from work_items)."""
    fields = {"descrptn": None, "adddesc": None, field: text}
    emitted, stats = _run_phase0(_packet(**fields))

    assert len(emitted) == 1
    decision = emitted[0]["decision"]
    assert decision["classification"] == "reclass"
    assert decision["basis"] == "reclass_rule"
    assert emitted[0]["phase"] == "phase0"
    assert "reclass_rule" in emitted[0]["flags"]
    assert stats["reclass_fired"] == 1
    assert stats["work_items"] == []  # never AI-bound -> can never reach human_review


def test_reclass_wins_over_negative_amount():
    """Precedence: a negative-amount reclass row is `reclass`, not
    `skipped_negative`."""
    emitted, stats = _run_phase0(_packet(descrptn="Reclass entry", amount=-500.0))
    assert emitted[0]["decision"]["classification"] == "reclass"
    assert stats["reclass_fired"] == 1
    assert stats["negatives_skipped"] == 0


def test_reclass_wins_over_closegl():
    """Precedence: a CLOSEGL reclass row is `reclass`, not auto-recurring, and
    the CLOSEGL deal-guard never gets a chance to route it to human_review."""
    emitted, stats = _run_phase0(_packet(descrptn="Reclass merger costs", userid="CLOSEGL"))
    assert emitted[0]["decision"]["classification"] == "reclass"
    assert stats["reclass_fired"] == 1
    assert stats["closegl_fired"] == 0
    assert stats["closegl_guard_trips"] == 0


def test_reclass_wins_over_ma_account_and_leaves_the_sweep():
    """Precedence: an M&A-account reclass row is `reclass` and is removed from
    the AI/sweep workload (not carried as a work_item to Phase 1)."""
    from gna_pipeline import config

    emitted, stats = _run_phase0(_packet(descrptn="Reclass", acctnum=config.MA_ACCTNUM))
    assert emitted[0]["decision"]["classification"] == "reclass"
    assert stats["work_items"] == []


def test_reclass_record_is_a_settled_decision_with_no_error_or_sweep_debt_flag(tmp_path):
    """A reclass record is a clean, final decision -- no error, no
    deal_sweep_failed/skipped flag -- durable via append_record/
    load_all_records like any other decision. (There is no resume state for
    it to be "included in" any more -- every run re-decides every row
    regardless; see persistence.py.)"""
    emitted, _ = _run_phase0(_packet(descrptn="Reclass entry"))
    path = tmp_path / "results.jsonl"
    persistence.append_record(path, emitted[0])
    reloaded = persistence.load_all_records(path)
    assert len(reloaded) == 1
    assert reloaded[0]["error"] is None
    assert not (set(reloaded[0]["flags"] or []) & {"deal_sweep_failed", "deal_sweep_skipped"})


def test_non_reclass_row_is_unaffected():
    """A row with no 'reclass' text flows on to the AI workload as before."""
    emitted, stats = _run_phase0(_packet(descrptn="Advisory services", adddesc=None))
    assert stats["reclass_fired"] == 0
    assert len(stats["work_items"]) == 1
    assert emitted == []  # AI-bound rows are not emitted in Phase 0


# ---------------------------------------------------------------------------
# 5: invoice-token mining golden table
# ---------------------------------------------------------------------------

def _load_cases() -> list[tuple[str, str | None]]:
    cases = []
    for line in CASES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        raw, _, expected = line.partition("=>")
        expected = expected.strip()
        cases.append((raw.rstrip(), None if expected == "NONE" else expected))
    return cases


@pytest.mark.parametrize("raw,expected", _load_cases())
def test_invoice_token_extraction(raw, expected):
    key, _truncated = invoice_mining.mine_invoice_key(
        _packet(descrptn=raw, adddesc=None, invoice_url=None)
    )
    assert key == expected
