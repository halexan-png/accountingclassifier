"""contract.py — the ONLY place row/record shapes are defined.

Every other module imports RowPacket, DecisionRecord, the enums, and
row_hash from here. Nothing outside this file may define a row or record
shape.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, TypedDict

# ---------------------------------------------------------------------------
# Enums & flags
# ---------------------------------------------------------------------------

Classification = Literal[
    "recurring", "non_recurring", "human_review", "skipped_negative", "reclass"
]
Basis = Literal[
    "closegl_rule", "invoice_content", "deal_profile", "row_text_routine", "none",
    "ma_account_rule", "reclass_rule",
]
Phase = Literal["phase0", "deal_profile", "classify"]
Flag = Literal[
    "closegl_user",         # system close entry, auto-recurring, not sent to AI
    "reclass_rule",         # row text contains "reclass"; auto-labeled reclass in Phase 0, never sent to AI
    "skipped_negative",     # negative USD Amount, skipped this run (netting deferred)
    "had_invoice",          # row references an invoice (scannedcopyurl OR mined id)
    "invoice_accessed",     # a document was read (URL fetched, or local PDF located + read)
    "invoice_unavailable",  # invoice referenced but not readable
    "deal_profile_match",   # matches a Phase-1 deal-profile entry — suspicion only
    "amount_blank",         # USD Amount was blank/formula-dead
    "deal_sweep_failed",    # sweep could not gather deal info for this row (API/parse failure)
    "deal_sweep_skipped",   # M&A row not swept (outside selected quarters, or profile reused)
    "basis_mismatch",       # model claimed basis="deal_profile" but recognized_deal was "none";
                            # downgraded to "none" — the real driver (e.g. the floor rule on the
                            # row's own text) isn't captured by any basis value, so this is honest
                            # rather than guessing which one applies
    "invoice_read_mismatch",# an invoice document WAS sent to the model, but the model's
                            # invoice_read field says otherwise (or it claimed to read one when
                            # none was sent) — its invoice-based reasoning is untrustworthy; surfaced
                            # for the auditor, not auto-retried
    "invoice_truncated",    # read window exceeded PAGE_FULL_READ_MAX; middle pages unread
                            # (only the first/last PAGE_EDGE_COUNT were read) — a deciding
                            # date/deal name in the middle was never seen
]

InvoiceKind = Literal["pdf", "text", "none", "error"]
InvoiceSource = Literal["url", "local_file"]
# What the model reports about the invoice it was (or was not) given. Cross-checked
# in classify._coerce_row against whether a document block was actually sent.
InvoiceRead = Literal["read", "unavailable", "none_attached"]
YesNo = Literal["yes", "no"]

# Version tag baked into every row_hash. Bump on any RowPacket business-field
# change. NOTE: there is no guard against a version bump — it silently
# re-decides (and re-charges for) every row on the next run, since the old
# hash no longer matches. That's a real cost, not a refused operation; bump
# ROW_HASH_VERSION deliberately, not casually.
ROW_HASH_VERSION = "v4"

# The invoice component of the hash when no document was read.
NO_INVOICE = "no_invoice"


# ---------------------------------------------------------------------------
# RowPacket — the ONLY row view any downstream module sees.
# ---------------------------------------------------------------------------

class RowPacket(TypedDict):
    row_idx: int            # 1-based Excel row in source
    period: str             # YYYYMM
    acctnum: str
    ref: str
    entityid: str
    department: str | None  # DEPARTMENT ('@' is a common literal value — keep as-is)
    category: str | None    # Category: GL label OR vendor/payee name (dual purpose)
    amount: float           # USD Amount (USD-normalized)
    amount_was_blank: bool
    currency: str           # OCURRCODE, uppercased; 'UNKNOWN' if blank
    descrptn: str | None    # DESCRPN
    adddesc: str | None     # ADDLDESC
    source: str             # SOURCE
    entrdate: str | None    # ENTRDATE as ISO date string
    userid: str | None      # USERID
    invoice_url: str | None # 'Image URL - Hyperlink'


# Business fields = everything in RowPacket except row_idx and amount_was_blank.
# Kept as an explicit exclusion set so row_hash never silently drifts if
# RowPacket gains a field.
_ROW_HASH_EXCLUDED_FIELDS = frozenset({"row_idx", "amount_was_blank"})


def row_hash(packet: RowPacket, invoice_sha256: str = NO_INVOICE) -> str:
    """SHA-256 audit-key fingerprint, persisted as `row_hash` on every
    DecisionRecord. NOT a resume/skip key — every run re-decides every
    in-scope row regardless of whether its hash matches an earlier run.

    Hashes sorted-key JSON of all RowPacket business fields (everything except
    row_idx and amount_was_blank) + invoice_sha256 (or the literal "no_invoice"
    when no document was read) + the literal version tag ROW_HASH_VERSION.
    """
    business_fields = {
        k: v for k, v in packet.items() if k not in _ROW_HASH_EXCLUDED_FIELDS
    }
    payload = {
        "fields": business_fields,
        "invoice_sha256": invoice_sha256,
        "version": ROW_HASH_VERSION,
    }
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# WorkItem — the Phase-0 → Phase-2 handoff unit (one per AI-bound row).
# Built by prep.prepare_rows(); consumed by scheduling.size_batches().
# Not persisted; DecisionRecord is the only persisted shape.
# ---------------------------------------------------------------------------

class WorkItem(TypedDict):
    packet: RowPacket
    row_hash: str                    # full row_hash (invoice sha included)
    had_invoice: YesNo
    invoice_accessed: YesNo
    flags: list[Flag]                # Phase-0-computed flags, carried onto the record
    invoice: dict | None             # invoice_read.InvoiceResult (holds text/bytes); None = no document
    est_input_tokens: int            # row text + overhead + invoice cost (batcher input)


def invoice_summary_for_record(item: "WorkItem") -> "InvoiceSummary | None":
    """InvoiceSummary when a document was read (kind pdf/text) OR the attempt
    failed (kind error — carries the fetch/resolution failure reason so
    results.jsonl explains WHY an invoice-bearing row went unread). None only
    when no invoice was referenced at all.

    Shared by classify.py (Phase 2) and deal_profile.py (Phase 1) — one
    invoice-summary rule for every DecisionRecord, whichever phase wrote it.
    Imports invoice_read locally: invoice_read imports InvoiceSummary from
    this module, so a module-level import here would be circular.
    """
    invoice = item.get("invoice")
    if not invoice or invoice.get("kind") not in ("pdf", "text", "error"):
        return None
    from gna_pipeline import invoice_read

    return invoice_read.to_summary(invoice)  # type: ignore[arg-type]


def vision_fallback_item(item: "WorkItem", reason: str) -> "WorkItem":
    """A shallow copy of `item` with its (vision) invoice downgraded to an
    `error` result carrying `reason`, and invoice_accessed set to "no" — used
    when a vision API call fails and the row is re-attempted as a text/row-only
    request. Because the downgraded invoice is kind "error" (not pdf/text),
    config.model_for_batch routes the re-attempt to the floor model and
    prompts._invoice_content_blocks emits a 'could not retrieve' notice instead
    of a PDF block, so the model decides on the row's own information. The
    error reason rides along on the record's invoice summary, so results.jsonl
    still explains WHY the invoice went unread."""
    from gna_pipeline import invoice_read

    inv = item.get("invoice") or {}
    new_item = dict(item)
    new_item["invoice"] = invoice_read.downgrade_to_error(inv, reason)
    new_item["invoice_accessed"] = "no"
    return new_item  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# DecisionRecord — the invariant per-row output.
# ---------------------------------------------------------------------------

class InvoiceSummary(TypedDict, total=False):
    kind: InvoiceKind
    source: InvoiceSource      # url = scannedcopyurl (priority); local_file = mined id
    path_or_url: str
    pages_read: str | None     # e.g. "1-4" — the range ACTUALLY read
    page_count: int | None     # total pages in the source document (pages_read < page_count => truncated)
    sha256: str | None
    size_bytes: int | None
    read_path: str | None      # "text" | "vision" — which extraction path was used
    error: str | None          # set when kind == "error"


class Decision(TypedDict, total=False):
    classification: Classification
    basis: Basis
    recognized_deal: str       # a recognized deal name, or "none"
    invoice_read: InvoiceRead  # did the model read an attached invoice? cross-checked vs what was sent
    invoice_date: str | None   # date read OUT of the invoice document (not entrdate); null if none/illegible
    reasoning: str             # 1-3 sentences, plain language
    evidence: str              # verbatim quotes, tagged [row] / [invoice p.N]
    missing_info: str | None   # REQUIRED non-null when classification == human_review
    override: dict[str, str]   # present only when the pipeline downgraded a model decision


class Usage(TypedDict, total=False):
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    usage_scope: str  # e.g. "batch_of_N" — attached to the FIRST record of a batch


class DecisionRecord(TypedDict):
    row_idx: int
    packet: RowPacket
    phase: Phase
    model_version: str | None
    error: str | None
    # --- human-aid columns (Phase 0; never fed to the AI) ---
    had_invoice: YesNo
    invoice_accessed: YesNo
    invoice: InvoiceSummary | None   # present only when a document was read; else None
    decision: Decision
    flags: list[Flag]                # pipeline-added; the model never sets flags
    row_hash: str
    usage: Usage


def zero_usage() -> Usage:
    return Usage(
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


def make_decision_record(
    *,
    packet: RowPacket,
    row_hash: str,
    phase: Phase,
    classification: Classification,
    basis: Basis,
    reasoning: str,
    evidence: str,
    had_invoice: YesNo = "no",
    invoice_accessed: YesNo = "no",
    invoice: InvoiceSummary | None = None,
    model_version: str | None = None,
    recognized_deal: str = "none",
    invoice_read: InvoiceRead = "none_attached",
    invoice_date: str | None = None,
    missing_info: str | None = None,
    flags: list[Flag] | None = None,
    usage: Usage | None = None,
    error: str | None = None,
) -> DecisionRecord:
    """Build a DecisionRecord. Callers still own doctrine (e.g. human_review must
    carry non-null missing_info) — this only assembles the shape.
    """
    decision: Decision = {
        "classification": classification,
        "basis": basis,
        "recognized_deal": recognized_deal,
        "invoice_read": invoice_read,
        "invoice_date": invoice_date,
        "reasoning": reasoning,
        "evidence": evidence,
        "missing_info": missing_info,
    }
    return DecisionRecord(
        row_idx=packet["row_idx"],
        packet=packet,
        phase=phase,
        model_version=model_version,
        error=error,
        had_invoice=had_invoice,
        invoice_accessed=invoice_accessed,
        invoice=invoice,
        decision=decision,
        flags=list(flags) if flags else [],
        row_hash=row_hash,
        usage=usage if usage is not None else zero_usage(),
    )


ERROR_REASONING = "Processing error; routed to human review."


def make_error_record(
    *,
    packet: RowPacket,
    row_hash: str,
    phase: Phase,
    error_msg: str,
    had_invoice: YesNo = "no",
    invoice_accessed: YesNo = "no",
    invoice: InvoiceSummary | None = None,
    flags: list[Flag] | None = None,
) -> DecisionRecord:
    """Never-drop-a-row fallback: any exception becomes a human_review record
    with `error` set and `missing_info` explaining, never a raise.
    """
    return make_decision_record(
        packet=packet,
        row_hash=row_hash,
        phase=phase,
        classification="human_review",
        basis="none",
        reasoning=ERROR_REASONING,
        evidence="none",
        missing_info=error_msg,
        had_invoice=had_invoice,
        invoice_accessed=invoice_accessed,
        invoice=invoice,
        flags=flags,
        usage=zero_usage(),
        error=error_msg,
    )
