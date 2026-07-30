"""prep.py — Phase-0 orchestrator: the zero-cost front of the pipeline.

Per-row order (binding):
    1. Classify-fate triage BEFORE any fetch, in precedence order:
         a. RECLASS rows FIRST — any row whose DESCRPN or ADDLDESC contains
            "reclass" (case-insensitive substring) is auto-labeled `reclass`
            (basis `reclass_rule`) and removed from the AI workload. This rule
            has the HIGHEST precedence: it fires ahead of the CLOSEGL, negative,
            and M&A-account paths, so a reclass row is labeled `reclass` even
            when it is also a CLOSEGL/negative/M&A row. It is unconditional and
            unguarded (unlike CLOSEGL) — a reclass row must NEVER reach human
            review, by design.
         b. CLOSEGL rows (`userid == "CLOSEGL"`, guarded by
            `closegl_guard_trips`) and negative-amount rows are then resolved
            mechanically here and removed from the AI workload.
       All of these still get an invoice *reference* (see deviation below).
    2. Remaining (AI-bound) rows get their invoice resolved per a strict
       priority: a `scannedcopyurl` is fetched and, on failure, is a hard
       null-invoice outcome (never falls back to local); otherwise a mined
       invoice key is resolved against the local directory.
    3. A `WorkItem` is built for every AI-bound row that made it through
       without raising; Phase 2 consumes `work_items` to size batches.

Every row is processed every run — there is no resume/skip path here. A row
already decided by a prior run is simply re-decided; the new results.jsonl
line supersedes the old one under last-write-wins (see persistence.py).

Deviation from a literal reading of "every row fetches an invoice": a
CLOSEGL/negative row is never sent to the AI, so paying for an HTTP fetch or a
local PDF read on its behalf buys nothing but latency. Those rows still get
`had_invoice` computed (URL present, or a key mines from the row text) so a
reviewer sees the same reference column as everywhere else, but
`invoice_accessed` is always "no" and `invoice_unavailable` is never set for
them (the fetch was simply never attempted, which is a different fact than
"attempted and failed"). Their `row_hash` therefore always uses
`contract.NO_INVOICE`.

Concurrency: URL fetches are pure HTTP ($0, no shared state) and run
concurrently across AI-bound rows via `ThreadPoolExecutor(url_workers)` in a
prefetch pass, deduplicated by URL. Local PDF reads and `emit()` (JSONL
append) stay on the main thread in a second, sequential pass — persistence
must not race, and local disk reads are already fast.

Never raises out of a row: any per-row exception becomes a `human_review`
error record (`contract.make_error_record`, phase "phase0") and the row is
excluded from `work_items`, never a crash of the whole run.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypedDict

from gna_pipeline import config, invoice_mining, invoice_read
from gna_pipeline.contract import (
    NO_INVOICE,
    DecisionRecord,
    Flag,
    RowPacket,
    WorkItem,
    YesNo,
    make_decision_record,
    make_error_record,
)
from gna_pipeline.contract import row_hash as compute_row_hash  # local var below is also named row_hash
from gna_pipeline.invoice_read import InvoiceResult
from gna_pipeline.prompts import estimate_row_tokens

logger = logging.getLogger("gna.prep")

_CLOSEGL_USERID = "CLOSEGL"
_CLOSEGL_GUARD_DESC_RE = re.compile(r"(?i)acquisi|disposi|merger|transaction|deal")

# Highest-precedence Phase-0 rule: any row whose DESCRPN or ADDLDESC carries
# "reclass" (case-insensitive substring — matches "Reclass", "RECLASS",
# "reclassification", "reclassified", etc.) is a bookkeeping reclassification
# entry, auto-labeled `reclass` and never sent to the AI.
_RECLASS_RE = re.compile(r"(?i)reclass")

_PROGRESS_EVERY = 100


def is_reclass_row(packet: RowPacket) -> bool:
    """True when the row's DESCRPN or ADDLDESC contains "reclass"
    (case-insensitive substring). These are bookkeeping reclassification
    entries: auto-labeled `reclass` in Phase 0, removed from the AI workload,
    and — unlike CLOSEGL — never guarded into human_review."""
    descrptn = packet.get("descrptn") or ""
    if _RECLASS_RE.search(descrptn):
        return True
    adddesc = packet.get("adddesc") or ""
    return bool(_RECLASS_RE.search(adddesc))


def closegl_guard_trips(packet: RowPacket) -> bool:
    """Guard: a CLOSEGL row must NOT be auto-cleared if it looks deal-related.
    True when `category` OR `descrptn` matches
    `(?i)acquisi|disposi|merger|transaction|deal`.
    """
    category = packet.get("category") or ""
    if _CLOSEGL_GUARD_DESC_RE.search(category):
        return True
    descrptn = packet.get("descrptn") or ""
    return bool(_CLOSEGL_GUARD_DESC_RE.search(descrptn))


def _is_substantive_reference(value: str) -> bool:
    """True when a raw invoice reference (invoice_url text, or a mined
    invoice key) has at least config.MIN_SUBSTANTIVE_REFERENCE_CHARS
    alphanumeric characters. A shorter one ("123", "a51") is noise, not a
    real pointer to a document — it must never be fetched/retried, and a
    failed lookup against one must never count toward invoice_read_failed
    (which is the honest "we had a real invoice and could not read it"
    total shown to the operator)."""
    return sum(1 for ch in value if ch.isalnum()) >= config.MIN_SUBSTANTIVE_REFERENCE_CHARS


class Phase0Stats(TypedDict):
    reclass_fired: int
    closegl_fired: int
    closegl_guard_trips: int
    negatives_skipped: int
    to_classify: int
    had_invoice_yes: int
    invoice_accessed_yes: int
    invoice_unavailable: int
    invoice_read_failed: int
    url_fetched_ok: int
    url_fetch_failed: int
    local_resolved: int
    local_no_match: int
    ambiguous: int
    errors: int
    work_items: list[WorkItem]


def _empty_stats() -> Phase0Stats:
    return Phase0Stats(
        reclass_fired=0,
        closegl_fired=0,
        closegl_guard_trips=0,
        negatives_skipped=0,
        to_classify=0,
        had_invoice_yes=0,
        invoice_accessed_yes=0,
        invoice_unavailable=0,
        invoice_read_failed=0,
        url_fetched_ok=0,
        url_fetch_failed=0,
        local_resolved=0,
        local_no_match=0,
        ambiguous=0,
        errors=0,
        work_items=[],
    )


def _has_invoice_reference(packet: RowPacket) -> bool:
    """Cheap had_invoice check for Phase-0-resolved rows: a URL is present, OR
    a key mines from the row text. Never fetches/reads anything."""
    if packet.get("invoice_url"):
        return True
    key, _truncated = invoice_mining.mine_invoice_key(packet)
    return key is not None


def _pretriage_flags(packet: RowPacket) -> list[Flag]:
    flags: list[Flag] = []
    if packet["amount_was_blank"]:
        flags.append("amount_blank")
    return flags


# A pending AI-bound row, carrying what step 2 already computed for it.
_PendingRow = tuple[RowPacket, list[Flag]]


def prepare_rows(
    packets: list[RowPacket],
    lookup_index: dict[str, list[dict]],
    *,
    emit: Callable[[DecisionRecord], None],
    fetch_urls: bool = True,
    url_workers: int = 8,
) -> Phase0Stats:
    """Run Phase 0 over every row. `lookup_index` is
    `invoice_mining.load_lookup_index`'s return value. `emit` is called once
    per Phase-0-resolved row (CLOSEGL / guard / negative / error) — AI-bound
    rows are NOT emitted here, they flow onward as `WorkItem`s in the
    returned stats for Phase 2 to decide and emit. Every row proceeds through
    Phase 0 every run — there is no resume/skip path.
    """
    stats = _empty_stats()
    pending: list[_PendingRow] = []
    total = len(packets)

    # --- Pass A: CLOSEGL/negative triage -------------------------------
    for i, packet in enumerate(packets, start=1):
        if i % _PROGRESS_EVERY == 0:
            logger.info("phase0: processed %d/%d rows", i, total)

        try:
            base_flags = _pretriage_flags(packet)

            # HIGHEST-PRECEDENCE RULE: a row whose text says "reclass" is a
            # bookkeeping reclassification entry -> auto-labeled `reclass`,
            # removed from the AI workload, NEVER human review. Checked ahead
            # of the CLOSEGL/negative/M&A paths so it wins over all of them.
            # Like the CLOSEGL/negative rows below, it never fetches/reads a
            # document (see module docstring deviation) -> row_hash is always
            # NO_INVOICE and invoice_accessed is always "no".
            if is_reclass_row(packet):
                had_invoice_ref = _has_invoice_reference(packet)
                flags = list(base_flags) + ["reclass_rule"]
                if had_invoice_ref:
                    flags.append("had_invoice")
                had_invoice: YesNo = "yes" if had_invoice_ref else "no"
                record = make_decision_record(
                    packet=packet,
                    row_hash=compute_row_hash(packet),
                    phase="phase0",
                    classification="reclass",
                    basis="reclass_rule",
                    reasoning=(
                        "Row description contains 'reclass'; auto-labeled as a "
                        "reclassification entry and not sent to the classifier."
                    ),
                    evidence=(
                        f"[row] descrptn={packet.get('descrptn')!r} "
                        f"adddesc={packet.get('adddesc')!r}"
                    ),
                    had_invoice=had_invoice,
                    invoice_accessed="no",
                    invoice=None,
                    model_version=None,
                    flags=flags,
                )
                emit(record)
                stats["reclass_fired"] += 1
                if had_invoice_ref:
                    stats["had_invoice_yes"] += 1
                continue

            userid_norm = (packet.get("userid") or "").strip().upper()
            is_closegl = userid_norm == _CLOSEGL_USERID
            is_negative = packet["amount"] < 0

            if is_closegl or is_negative:
                had_invoice_ref = _has_invoice_reference(packet)
                flags = list(base_flags)
                if had_invoice_ref:
                    flags.append("had_invoice")
                had_invoice: YesNo = "yes" if had_invoice_ref else "no"
                # Phase-0-resolved rows never fetch/read a document (see
                # module docstring deviation) -> row_hash always NO_INVOICE.
                row_hash = compute_row_hash(packet)

                if is_closegl:
                    flags.append("closegl_user")
                    if closegl_guard_trips(packet):
                        record = make_decision_record(
                            packet=packet,
                            row_hash=row_hash,
                            phase="phase0",
                            classification="human_review",
                            basis="none",
                            reasoning=(
                                "CLOSEGL row's category/description reads as "
                                "deal-related; the CLOSEGL auto-recurring guard "
                                "tripped instead of auto-clearing this row."
                            ),
                            evidence=(
                                f"[row] category={packet.get('category')!r} "
                                f"descrptn={packet.get('descrptn')!r}"
                            ),
                            had_invoice=had_invoice,
                            invoice_accessed="no",
                            invoice=None,
                            model_version=None,
                            missing_info=(
                                "CLOSEGL guard tripped: category or description "
                                "suggests a deal/transaction entry; needs human "
                                "confirmation before auto-clearing as recurring."
                            ),
                            flags=flags,
                        )
                        emit(record)
                        stats["closegl_guard_trips"] += 1
                    else:
                        record = make_decision_record(
                            packet=packet,
                            row_hash=row_hash,
                            phase="phase0",
                            classification="recurring",
                            basis="closegl_rule",
                            reasoning=(
                                "System close entry (userid CLOSEGL); "
                                "auto-classified recurring."
                            ),
                            evidence="system close entry (userid CLOSEGL)",
                            had_invoice=had_invoice,
                            invoice_accessed="no",
                            invoice=None,
                            model_version=None,
                            flags=flags,
                        )
                        emit(record)
                        stats["closegl_fired"] += 1
                else:
                    flags.append("skipped_negative")
                    record = make_decision_record(
                        packet=packet,
                        row_hash=row_hash,
                        phase="phase0",
                        classification="skipped_negative",
                        basis="none",
                        reasoning="negative amount; netting deferred (v3_00 §7)",
                        evidence="none",
                        had_invoice=had_invoice,
                        invoice_accessed="no",
                        invoice=None,
                        model_version=None,
                        flags=flags,
                    )
                    emit(record)
                    stats["negatives_skipped"] += 1

                if had_invoice_ref:
                    stats["had_invoice_yes"] += 1
                continue

            # AI-bound: invoice resolution deferred to Passes B/C below.
            pending.append((packet, base_flags))

        except Exception as e:  # noqa: BLE001 — never drop a row
            logger.exception(
                "phase0: error resolving row_idx=%s", packet.get("row_idx")
            )
            emit(
                make_error_record(
                    packet=packet,
                    row_hash=compute_row_hash(packet),
                    phase="phase0",
                    error_msg=f"phase0_resolution_error: {e}",
                )
            )
            stats["errors"] += 1

    # --- Pass B: concurrent URL prefetch, dedup'd by URL --------------------
    # A non-substantive invoice_url ("123", "a51") is never a real link — skip
    # it here so it never reaches the network/retry loop; Pass C below
    # synthesizes a "reference_too_short" result for it instead.
    url_results: dict[str, InvoiceResult] = {}
    if fetch_urls:
        unique_urls = sorted(
            {
                p["invoice_url"] for p, _flags in pending
                if p.get("invoice_url") and _is_substantive_reference(p["invoice_url"])
            }
        )
        if unique_urls:
            with ThreadPoolExecutor(max_workers=url_workers) as pool:
                fetched = list(pool.map(invoice_read.fetch_invoice_url, unique_urls))
            url_results = dict(zip(unique_urls, fetched))

    # --- Pass C: sequential per-row invoice resolution + WorkItem build -----
    for packet, base_flags in pending:
        try:
            flags = list(base_flags)
            invoice_url = packet.get("invoice_url")
            had_invoice: YesNo
            invoice_accessed: YesNo
            invoice_obj: InvoiceResult | None
            row_hash: str
            est: int

            if invoice_url:
                # Priority source: a scannedcopyurl IS the invoice. Never
                # falls back to local resolution for a URL-bearing row.
                if fetch_urls:
                    substantive_url = _is_substantive_reference(invoice_url)
                    if substantive_url:
                        result = url_results.get(invoice_url) or invoice_read.fetch_invoice_url(
                            invoice_url
                        )
                    else:
                        # Too short to be a real link ("123", "a51") — never
                        # fetched/retried over the network (see Pass B above).
                        result = invoice_read.unavailable_invoice(
                            "url", invoice_url, "reference_too_short"
                        )
                    if result.get("kind") in ("pdf", "text"):
                        had_invoice = "yes"
                        invoice_accessed = "yes"
                        flags += ["had_invoice", "invoice_accessed"]
                        invoice_obj = result
                        row_hash = compute_row_hash(packet, result.get("sha256") or NO_INVOICE)
                        est = estimate_row_tokens(packet) + result.get("est_input_tokens", 0)
                        stats["url_fetched_ok"] += 1
                    else:
                        had_invoice = "yes"
                        invoice_accessed = "no"
                        flags += ["had_invoice", "invoice_unavailable"]
                        # Keep the error result: the fetch failure reason must
                        # survive into results.jsonl, not vanish with a None.
                        invoice_obj = result
                        row_hash = compute_row_hash(packet)
                        est = estimate_row_tokens(packet)
                        stats["url_fetch_failed"] += 1
                        stats["invoice_unavailable"] += 1
                        # invoice_read_failed excludes the too-short/noise
                        # references above — it's the operator-facing count of
                        # rows that had a REAL invoice reference we couldn't read.
                        if substantive_url:
                            stats["invoice_read_failed"] += 1
                else:
                    # --fetch-urls=False: charge a placeholder cost for
                    # batching honesty; never a real fetch, never
                    # invoice_unavailable (never attempted, not failed).
                    had_invoice = "yes"
                    invoice_accessed = "no"
                    flags += ["had_invoice"]
                    invoice_obj = None
                    row_hash = compute_row_hash(packet)
                    est = estimate_row_tokens(packet) + 2 * config.VISION_TOKENS_PER_PAGE_MID

            else:
                mined_key, truncated = invoice_mining.mine_invoice_key(packet)
                if mined_key is None:
                    had_invoice = "no"
                    invoice_accessed = "no"
                    invoice_obj = None
                    row_hash = compute_row_hash(packet)
                    est = estimate_row_tokens(packet)
                else:
                    had_invoice = "yes"
                    flags.append("had_invoice")
                    path, pages, status = invoice_mining.resolve_local(
                        mined_key,
                        truncated,
                        packet.get("entityid"),
                        config.INVOICE_DIR,
                        lookup_index,
                    )
                    if status == "resolved":
                        stats["local_resolved"] += 1
                        result = invoice_read.read_local_invoice(path, pages)
                        if result.get("kind") in ("pdf", "text"):
                            invoice_accessed = "yes"
                            flags.append("invoice_accessed")
                            invoice_obj = result
                            row_hash = compute_row_hash(
                                packet, result.get("sha256") or NO_INVOICE
                            )
                            est = estimate_row_tokens(packet) + result.get(
                                "est_input_tokens", 0
                            )
                        else:
                            invoice_accessed = "no"
                            flags.append("invoice_unavailable")
                            invoice_obj = result  # keep the read-failure reason
                            row_hash = compute_row_hash(packet)
                            est = estimate_row_tokens(packet)
                            stats["invoice_unavailable"] += 1
                            if _is_substantive_reference(mined_key):
                                stats["invoice_read_failed"] += 1
                    else:
                        if status == "no_match":
                            stats["local_no_match"] += 1
                        elif status == "ambiguous":
                            stats["ambiguous"] += 1
                        invoice_accessed = "no"
                        flags.append("invoice_unavailable")
                        invoice_obj = invoice_read.unavailable_invoice(
                            "local_file", mined_key, f"local_resolution_{status}"
                        )
                        row_hash = compute_row_hash(packet)
                        est = estimate_row_tokens(packet)
                        stats["invoice_unavailable"] += 1
                        if _is_substantive_reference(mined_key):
                            stats["invoice_read_failed"] += 1

            if had_invoice == "yes":
                stats["had_invoice_yes"] += 1
            if invoice_accessed == "yes":
                stats["invoice_accessed_yes"] += 1

            work_item: WorkItem = {
                "packet": packet,
                "row_hash": row_hash,
                "had_invoice": had_invoice,
                "invoice_accessed": invoice_accessed,
                "flags": flags,
                "invoice": invoice_obj,
                "est_input_tokens": est,
            }
            stats["work_items"].append(work_item)

        except Exception as e:  # noqa: BLE001 — never drop a row
            logger.exception(
                "phase0: error resolving invoice for row_idx=%s", packet.get("row_idx")
            )
            emit(
                make_error_record(
                    packet=packet,
                    row_hash=compute_row_hash(packet),
                    phase="phase0",
                    error_msg=f"phase0_invoice_resolution_error: {e}",
                )
            )
            stats["errors"] += 1

    stats["to_classify"] = len(stats["work_items"])
    logger.info(
        "phase0 complete: reclass=%d closegl=%d guard_trips=%d negatives=%d "
        "to_classify=%d errors=%d had_invoice=%d invoice_accessed=%d "
        "invoice_unavailable=%d invoice_read_failed=%d",
        stats["reclass_fired"],
        stats["closegl_fired"],
        stats["closegl_guard_trips"],
        stats["negatives_skipped"],
        stats["to_classify"],
        stats["errors"],
        stats["had_invoice_yes"],
        stats["invoice_accessed_yes"],
        stats["invoice_unavailable"],
        stats["invoice_read_failed"],
    )
    return stats
