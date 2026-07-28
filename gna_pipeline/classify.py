"""classify.py — Phase-2 token-budget batch classifier.

Implements:
  - `forecast` — the mandatory pre-run cost/time estimate, pure (no IO, no
    printing — `pipeline.py` prints it).
  - `run_classification` — the rate-limited, crash-safe classify loop: one
    `client.messages.create` per batch, forced tool `classify_rows`,
    anti-conflation parsing, split-and-retry on parse/size failure, and the
    unreadable-invoice-with-named-deal safety downgrade.

Batch sizing, the rate limiter, worker-count derivation, and the warmup +
concurrent-pool driver live in `scheduling.py`, shared with
`deal_profile.run_sweep` (Phase 1) — one scheduler, not two.

Never-drop-a-row / never-raise-out-of-a-worker are load-bearing: every path
through `_process_batch` ends in a DecisionRecord for every row it was asked
to classify, emitted the instant its batch (or retry-split sub-batch) lands.
"""

from __future__ import annotations

import itertools
import logging
import math
import threading
from collections import Counter
from typing import Any, Callable

import anthropic

from gna_pipeline import config, console, prompts, scheduling
from gna_pipeline.contract import (
    DecisionRecord,
    WorkItem,
    invoice_summary_for_record,
    make_decision_record,
    make_error_record,
    vision_fallback_item,
    zero_usage,
)
from gna_pipeline.scheduling import SpendCapExceeded

logger = logging.getLogger("gna.classify")

# The AI-tolerated output sets — deliberately NARROWER than the full
# contract enums. "reclass"/"reclass_rule" and the other Phase-0-only labels
# (skipped_negative, ma_account_rule, closegl_rule) are set mechanically
# before a row ever reaches the classifier, so the model must never emit them;
# a model row claiming one is treated as invalid by _coerce_row below.
_VALID_CLASSIFICATIONS = ("recurring", "non_recurring", "human_review")
_VALID_BASIS = ("closegl_rule", "invoice_content", "deal_profile", "row_text_routine", "none")
_VALID_INVOICE_READ = ("read", "unavailable", "none_attached")

_UNVERIFIED_DEAL_OVERRIDE_REASON = (
    "invoice referenced but unreadable; a named-deal claim that could not be checked "
    "against the invoice must not pass as a clean classification"
)


def forecast(
    items: list[WorkItem], model: str, rate_limits: dict | None
) -> dict[str, Any]:
    """Pre-run cost/time estimate. Pure — does no IO and prints nothing;
    `pipeline.py` is responsible for printing/confirming."""
    rows = len(items)

    # Two-tier split, mirroring config.model_for_batch's own per-row test:
    # invoice-bearing rows run (and price) at INVOICE_MODEL; every other row
    # at the floor (`model`). Each row's own tokens are already exactly
    # attributable to one tier or the other, so this splits cleanly with no
    # approximation (unlike deal_profile.sweep_forecast, which also carries a
    # shared per-batch overhead).
    invoice_items: list[WorkItem] = []
    floor_items: list[WorkItem] = []
    for it in items:
        inv = it.get("invoice") or {}
        (invoice_items if inv.get("kind") in ("pdf", "text") else floor_items).append(it)
    rows_with_invoice = len(invoice_items)

    target_tokens = config.batch_target_tokens(rate_limits)
    est_batches = len(scheduling.size_batches(items, target_tokens))

    input_tokens_high = sum(it.get("est_input_tokens", 0) for it in items)
    input_tokens_low = sum(scheduling.low_end_tokens(it) for it in items)

    output_tokens_est = rows * config.MAX_TOKENS_PER_ROW_OUTPUT

    pricing_invoice = config.pricing_for(config.INVOICE_MODEL)
    pricing_floor = config.pricing_for(model)

    invoice_input_low = sum(scheduling.low_end_tokens(it) for it in invoice_items)
    invoice_input_high = sum(it.get("est_input_tokens", 0) for it in invoice_items)
    invoice_output = rows_with_invoice * config.MAX_TOKENS_PER_ROW_OUTPUT

    floor_input_low = sum(scheduling.low_end_tokens(it) for it in floor_items)
    floor_input_high = sum(it.get("est_input_tokens", 0) for it in floor_items)
    floor_output = (rows - rows_with_invoice) * config.MAX_TOKENS_PER_ROW_OUTPUT

    invoice_cost_low = (
        invoice_input_low / 1_000_000 * pricing_invoice["input"]
        + invoice_output / 1_000_000 * pricing_invoice["output"]
    )
    invoice_cost_high = (
        invoice_input_high / 1_000_000 * pricing_invoice["input"]
        + invoice_output / 1_000_000 * pricing_invoice["output"]
    )
    floor_cost_low = (
        floor_input_low / 1_000_000 * pricing_floor["input"]
        + floor_output / 1_000_000 * pricing_floor["output"]
    )
    floor_cost_high = (
        floor_input_high / 1_000_000 * pricing_floor["input"]
        + floor_output / 1_000_000 * pricing_floor["output"]
    )

    cost_low_usd = invoice_cost_low + floor_cost_low
    cost_high_usd = invoice_cost_high + floor_cost_high

    # Transparency only -- cost_low_usd/cost_high_usd above already sum both
    # tiers; pipeline.py's forecast print can show this line separately.
    cost_by_tier = {
        "invoice": {
            "model": config.INVOICE_MODEL,
            "rows": rows_with_invoice,
            "cost_low_usd": round(invoice_cost_low, 4),
            "cost_high_usd": round(invoice_cost_high, 4),
        },
        "floor": {
            "model": model,
            "rows": rows - rows_with_invoice,
            "cost_low_usd": round(floor_cost_low, 4),
            "cost_high_usd": round(floor_cost_high, 4),
        },
    }

    # concurrency_wall is what `workers` concurrent batches actually take at
    # SCHED_CLASSIFY_LATENCY_S each (batch 1 always alone, warmup); the
    # budget walls below are additional floors from whichever of
    # RPM/ITPM/OTPM is actually measured — the real run can't go faster than
    # any of them, so the forecast is the max, not just one.
    workers = scheduling.compute_max_workers(
        rate_limits,
        batch_target_tokens=target_tokens,
        est_output_per_batch=output_tokens_est // max(1, est_batches),
        latency_s=config.SCHED_CLASSIFY_LATENCY_S,
        n_batches=est_batches,
    )
    latency_s = config.SCHED_CLASSIFY_LATENCY_S
    if est_batches >= 1:
        concurrency_wall_min = (
            latency_s + math.ceil(max(0, est_batches - 1) / workers) * latency_s
        ) / 60.0
    else:
        concurrency_wall_min = 0.0

    wall_candidates = [concurrency_wall_min]
    if rate_limits:
        itpm = rate_limits.get("input_tokens_limit")
        if isinstance(itpm, (int, float)) and itpm > 0:
            wall_candidates.append(input_tokens_high / (0.8 * itpm))
        rpm = rate_limits.get("requests_limit")
        if isinstance(rpm, (int, float)) and rpm > 0:
            wall_candidates.append(est_batches / (0.8 * rpm))
        otpm = rate_limits.get("output_tokens_limit")
        if isinstance(otpm, (int, float)) and otpm > 0:
            wall_candidates.append(output_tokens_est / (0.8 * otpm))
    wall_clock_est_min = max(wall_candidates)

    return {
        "rows": rows,
        "rows_with_invoice": rows_with_invoice,
        "est_batches": est_batches,
        "input_tokens_low": input_tokens_low,
        "input_tokens_high": input_tokens_high,
        "output_tokens_est": output_tokens_est,
        "cost_low_usd": cost_low_usd,
        "cost_high_usd": cost_high_usd,
        "cost_by_tier": cost_by_tier,
        "wall_clock_est_min": wall_clock_est_min,
        "max_workers": workers,
    }


# ---------------------------------------------------------------------------
# Per-row record assembly — tolerant reads + safety net.
# ---------------------------------------------------------------------------

def _error_record_for_item(item: WorkItem, error_msg: str) -> DecisionRecord:
    return make_error_record(
        packet=item["packet"],
        row_hash=item["row_hash"],
        phase="classify",
        error_msg=error_msg,
        had_invoice=item["had_invoice"],
        invoice_accessed=item["invoice_accessed"],
        invoice=invoice_summary_for_record(item),
        flags=list(item.get("flags") or []),
    )


def _coerce_row(item: WorkItem, model_row: dict[str, Any], model: str) -> DecisionRecord:
    """Build one DecisionRecord from a WorkItem + its model output object.

    Tolerant of malformed enum values (coerced to a safe default); a missing
    or invalid `classification` is not tolerated — that row becomes an error
    record on its own ("never drop a row", applied per-row).
    """
    flags = list(item.get("flags") or [])

    classification = model_row.get("classification")
    if classification not in _VALID_CLASSIFICATIONS:
        return _error_record_for_item(
            item, f"model returned invalid/missing classification: {classification!r}"
        )

    basis = model_row.get("basis")
    if basis not in _VALID_BASIS:
        basis = "none"

    recognized_deal = model_row.get("recognized_deal")
    if not isinstance(recognized_deal, str) or not recognized_deal.strip():
        recognized_deal = "none"

    reasoning = model_row.get("reasoning")
    if not isinstance(reasoning, str):
        reasoning = ""

    evidence = model_row.get("evidence")
    if not isinstance(evidence, str):
        evidence = ""

    missing_info = model_row.get("missing_info")
    if not isinstance(missing_info, str):
        missing_info = None

    # Invoice-read cross-check: what the model SAYS it did with the invoice vs.
    # what was actually sent. A document block is present only for kind pdf/text;
    # kind "error"/"none" (or no invoice at all) means none was sent.
    invoice = item.get("invoice") or {}
    doc_sent = invoice.get("kind") in ("pdf", "text")

    raw_invoice_read = model_row.get("invoice_read")
    invoice_read_valid = raw_invoice_read in _VALID_INVOICE_READ
    # Flag (never auto-retry — a retry spends money on a soft signal) when a
    # document was sent but the model claims it wasn't read, or no document was
    # sent but the model claims it read one (a fabrication signal).
    if invoice_read_valid and (
        (doc_sent and raw_invoice_read != "read")
        or (not doc_sent and raw_invoice_read == "read")
    ) and "invoice_read_mismatch" not in flags:
        flags = flags + ["invoice_read_mismatch"]
    invoice_read_val = (
        raw_invoice_read if invoice_read_valid
        else ("read" if doc_sent else "none_attached")
    )

    invoice_date = model_row.get("invoice_date")
    if not isinstance(invoice_date, str) or not invoice_date.strip():
        invoice_date = None

    # A5: an invoice read only up to its page cap can hide a later date/deal name.
    from gna_pipeline import invoice_read as _invoice_read
    if (
        doc_sent
        and _invoice_read.was_truncated(invoice)
        and "invoice_truncated" not in flags
    ):
        flags = flags + ["invoice_truncated"]

    if recognized_deal not in ("", "none") and "deal_profile_match" not in flags:
        flags = flags + ["deal_profile_match"]

    if basis == "deal_profile" and recognized_deal in ("", "none"):
        basis = "none"
        if "basis_mismatch" not in flags:
            flags = flags + ["basis_mismatch"]

    override = None

    # A fetch that was attempted and FAILED is not the same fact as no invoice
    # ever being referenced: reading the actual invoice content (which might
    # contradict a row's deal claim, per doctrine Q1) never happened here
    # either way, but only this case had a document to check against. Don't
    # let a named-deal classification stand on row text alone when a
    # readable invoice might have contradicted it.
    if (
        "invoice_unavailable" in flags
        and recognized_deal not in ("", "none")
        and classification != "human_review"
    ):
        override = {"from": classification, "reason": _UNVERIFIED_DEAL_OVERRIDE_REASON}
        classification = "human_review"

    if classification == "human_review" and not missing_info:
        missing_info = "model omitted missing_info"

    record = make_decision_record(
        packet=item["packet"],
        row_hash=item["row_hash"],
        phase="classify",
        classification=classification,
        basis=basis,
        reasoning=reasoning,
        evidence=evidence,
        had_invoice=item["had_invoice"],
        invoice_accessed=item["invoice_accessed"],
        invoice=invoice_summary_for_record(item),
        model_version=model,
        recognized_deal=recognized_deal,
        invoice_read=invoice_read_val,
        invoice_date=invoice_date,
        missing_info=missing_info,
        flags=flags,
    )
    if override is not None:
        record["decision"]["override"] = override
    return record


# ---------------------------------------------------------------------------
# Request + parse + split-retry
# ---------------------------------------------------------------------------

def _make_request(client: Any, batch: list[WorkItem], system_blocks: list[dict[str, Any]], model: str) -> Any:
    # Flat, generous output ceiling: max_tokens is free unless actually
    # generated, so the cap's only job is being unreachable — the
    # old per-row formula (200 + 350*rows) truncated verbose batches, and
    # every truncation became a paid split-retry cascade. The terse output
    # style (BATCH_INSTRUCTION + classifier.md) keeps real generation far
    # below this.
    kwargs: dict[str, Any] = dict(
        model=model,
        max_tokens=config.MAX_TOKENS_CLASSIFY_BATCH,
        system=system_blocks,
        messages=[{"role": "user", "content": prompts.build_batch_user_content(batch)}],
        tools=[prompts.CLASSIFY_ROWS_TOOL],
        tool_choice={"type": "tool", "name": "classify_rows"},
        # Sonnet 5 runs adaptive thinking by default when `thinking` is
        # omitted -- disabled here to preserve the deterministic forced-tool
        # extraction every batch depends on; a no-op on Sonnet 4.6.
        thinking={"type": "disabled"},
    )
    if config.supports_sampling_params(model):
        kwargs["temperature"] = 0
    # Vision batches are always single-row (size_batches isolates them) and a
    # stuck PDF read is rarely transient — cap them at VISION_MAX_RETRIES
    # (0 = one attempt, no second vision wait; a failure falls back to
    # text/row-only in _process_batch) instead of the client-wide budget.
    is_vision = any((it.get("invoice") or {}).get("read_path") == "vision" for it in batch)
    if is_vision:
        client = client.with_options(max_retries=config.VISION_MAX_RETRIES)
    return client.messages.create(**kwargs)


def _process_batch(
    client: Any,
    batch: list[WorkItem],
    system_blocks: list[dict[str, Any]],
    model: str,
    limiter: scheduling.RateLimiter,
    emit_locked: Callable[[DecisionRecord], None],
    batch_seq: itertools.count,
) -> list[DecisionRecord]:
    """Classify one batch. NEVER raises. Returns a DecisionRecord for every
    row in `batch`, each already emitted via `emit_locked`.

    Parse failure / no tool_use / a request-too-large API error -> split the
    batch in half and retry each half (recursion), bottoming out at a
    per-row error record for a 1-item batch that still fails. Any other
    exception after the SDK's own retries -> an error record for every row
    in `batch` directly, no split.
    """
    if not batch:
        return []

    # Per-batch model routing: `model` is the FLOOR the caller resolved
    # (config.DEFAULT_MODEL or a --model override); a batch holding any
    # readable invoice (kind pdf/text) upgrades to config.INVOICE_MODEL. Two
    # sub-batches from a split-retry each recompute this independently, since
    # a split can separate invoice rows from floor rows.
    batch_model = config.model_for_batch(batch, model)

    est_tokens = sum(it.get("est_input_tokens", 0) for it in batch)
    limiter.acquire(est_tokens, len(batch) * config.MAX_TOKENS_PER_ROW_OUTPUT)
    batch_id = next(batch_seq)

    vision_rows = sum(
        1 for it in batch if (it.get("invoice") or {}).get("read_path") == "vision"
    )
    row_idxs = [it["packet"]["row_idx"] for it in batch]
    row_idxs_str = str(row_idxs[:10]) + (f" +{len(row_idxs) - 10} more" if len(row_idxs) > 10 else "")
    # DEBUG, not INFO: this signal lives in the live status line (in-flight
    # count, running cost) instead of scrolling by per-batch.
    logger.debug(
        "gna.classify: batch idx=%d rows=%d est_tokens=%d vision_rows=%d model=%s row_idxs=%s -> request in flight",
        batch_id, len(batch), est_tokens, vision_rows, batch_model, row_idxs_str,
    )

    def fail_or_fallback(reason: str) -> list[DecisionRecord]:
        """A vision batch (always single-row) that fails its image request is
        NOT error-recorded: downgrade the invoice to text/row-only and re-run
        ONCE on the floor model (vision_fallback_item), so the row is classified
        from its own information with a 'could not retrieve invoice' notice.
        config.VISION_MAX_RETRIES is 0, so the image request never retried and
        never waited a second timeout. A non-vision batch — including that
        downgraded re-run — is error-recorded (human_review) as before."""
        if vision_rows:
            logger.warning(
                "gna.classify: batch idx=%d vision row(s)=%s failed (%s) -- "
                "retrying as text/row-only on the floor model",
                batch_id, row_idxs, reason,
            )
            fallback = [vision_fallback_item(it, reason) for it in batch]
            return _process_batch(
                client, fallback, system_blocks, model, limiter, emit_locked, batch_seq,
            )
        records = []
        for item in batch:
            record = _error_record_for_item(item, reason)
            emit_locked(record)
            records.append(record)
        return records

    def split_and_retry(reason: str) -> list[DecisionRecord]:
        if len(batch) == 1:
            return fail_or_fallback(reason)
        logger.warning(
            "gna.classify: batch idx=%d rows=%d split-retry (%s)", batch_id, len(batch), reason
        )
        mid = len(batch) // 2
        left = _process_batch(client, batch[:mid], system_blocks, model, limiter, emit_locked, batch_seq)
        right = _process_batch(client, batch[mid:], system_blocks, model, limiter, emit_locked, batch_seq)
        return left + right

    try:
        resp = _make_request(client, batch, system_blocks, batch_model)
    except anthropic.RequestTooLargeError as exc:
        return split_and_retry(f"request_too_large: {exc}")
    except Exception as exc:  # noqa: BLE001 — never raise out of a worker
        logger.warning(
            "gna.classify: batch idx=%d rows=%d API exception: %s",
            batch_id, len(batch), exc,
        )
        return fail_or_fallback(f"API call failed after retries: {exc}")

    # Response-shape parsing is defensive end to end: an SDK response object
    # that doesn't look like what we expect (odd content/usage shape) must
    # never raise out of this worker — treat it the same as any other
    # post-request exception (error-record every row, no split; a shape this
    # broken is not a "batch too big" signal).
    try:
        # Truncated output is a real (if now near-unreachable, given the flat
        # generous cap) failure mode: the tool JSON is cut mid-object. A split
        # halves the output each half needs, so it is the correct recovery —
        # and distinguishable in the logs from model misbehavior.
        if getattr(resp, "stop_reason", None) == "max_tokens":
            return split_and_retry(
                f"output truncated at max_tokens={config.MAX_TOKENS_CLASSIFY_BATCH}"
            )
        tool_block = next(
            (
                b for b in resp.content
                if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == "classify_rows"
            ),
            None,
        )
        if tool_block is None:
            return split_and_retry("no classify_rows tool_use block in response")

        payload = tool_block.input if isinstance(tool_block.input, dict) else None
        rows = payload.get("rows") if payload else None
        if not isinstance(rows, list) or not rows:
            return split_and_retry("tool_use payload missing a non-empty rows array")

        rows_by_idx: dict[int, dict[str, Any]] = {}
        for r in rows:
            if not isinstance(r, dict) or "row_idx" not in r:
                return split_and_retry("malformed row object in rows array")
            rows_by_idx[r["row_idx"]] = r

        expected_idxs = {it["packet"]["row_idx"] for it in batch}
        # ANTI-CONFLATION: exact set match AND no duplicate row_idx collapsing a
        # missing member.
        if set(rows_by_idx.keys()) != expected_idxs or len(rows) != len(rows_by_idx):
            return split_and_retry("row_idx set mismatch (anti-conflation)")

        usage = resp.usage
        records: list[DecisionRecord] = []
        for i, item in enumerate(batch):
            row_idx = item["packet"]["row_idx"]
            record = _coerce_row(item, rows_by_idx[row_idx], batch_model)
            if i == 0:
                record["usage"] = {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
                    "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
                    "usage_scope": f"batch_of_{len(batch)}",
                }
            else:
                record["usage"] = zero_usage()
            emit_locked(record)
            records.append(record)

        logger.debug(
            "gna.classify: batch idx=%d rows=%d est_tokens=%d actual_input=%d actual_output=%d",
            batch_id, len(batch), est_tokens, usage.input_tokens, usage.output_tokens,
        )
        return records
    except Exception as exc:  # noqa: BLE001 — never raise out of a worker
        logger.warning(
            "gna.classify: batch idx=%d rows=%d response-parse exception: %s",
            batch_id, len(batch), exc,
        )
        return fail_or_fallback(f"response parsing failed: {exc}")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_classification(
    client: Any,
    items: list[WorkItem],
    system_blocks: list[dict[str, Any]],
    *,
    model: str,
    rate_limits: dict | None,
    max_workers: int | None = None,
    cost_cap_usd: float | None = None,
    emit: Callable[[DecisionRecord], None],
) -> list[DecisionRecord]:
    """Classify every WorkItem in token-budgeted batches.

    Batch 1 runs alone, synchronously (warmup — writes the prompt cache);
    the rest run on a ThreadPoolExecutor(max_workers) behind a shared rate
    limiter, via the shared `scheduling.run_batches` scheduler. `emit` is
    called under a lock so concurrent batches' writes never interleave.
    Returns every DecisionRecord produced, in no particular order (each
    stands alone; row_idx identifies it).

    `max_workers=None` (the normal case) derives the worker count from
    measured rate limits via `scheduling.compute_max_workers`; pass an
    explicit int to override.

    `cost_cap_usd` (the runtime spend rail): when set, the accumulated
    actual cost is checked after every completed batch and a
    SpendCapExceeded raises out of `scheduling.run_batches` (which cancels
    queued batches first) — callers catch it like a KeyboardInterrupt.
    Everything already decided stays durable via `emit`.
    """
    if rate_limits is None:
        console.warn(
            f"no measured rate limits (rules/rate_limits.json absent) -- capping "
            f"max_workers at {config.MAX_WORKERS_UNMEASURED}; run `probe-limits` "
            f"first for a faster run"
        )

    emit_lock = threading.Lock()

    def emit_locked(record: DecisionRecord) -> None:
        with emit_lock:
            emit(record)

    if not items:
        return []

    target_tokens = config.batch_target_tokens(rate_limits)
    batches = scheduling.size_batches(items, target_tokens)
    if not batches:
        return []

    if max_workers is None:
        avg_output = len(items) * config.MAX_TOKENS_PER_ROW_OUTPUT // max(1, len(batches))
        max_workers = scheduling.compute_max_workers(
            rate_limits,
            batch_target_tokens=target_tokens,
            est_output_per_batch=avg_output,
            latency_s=config.SCHED_CLASSIFY_LATENCY_S,
            n_batches=len(batches),
        )

    limiter = scheduling.build_limiter(rate_limits)
    batch_seq = itertools.count(1)

    results: list[DecisionRecord] = []

    n_batches = len(batches)
    total_rows = len(items)
    console.kv([
        ("rows", str(total_rows)),
        ("batches", str(n_batches)),
        ("max_workers", f"{max_workers} (derived; ceiling {config.MAX_WORKERS_CEILING})"),
        ("warmup", "batch 1 runs alone to warm the prompt cache"),
    ])

    progress = console.Progress(total_rows=total_rows, total_batches=n_batches, model=model, unit="rows")

    # Per-batch cost, NOT one blended pricing.pricing_for(model) rate: a batch
    # can run on the floor or on config.INVOICE_MODEL (config.model_for_batch,
    # decided inside _process_batch), and the two prices differ. running_cost
    # is the spend-rail's own ground truth (deliberately independent of
    # console.Progress's internal cost tracking — money-critical accounting
    # doesn't depend on the presentation layer); cost_by_model is carried only
    # for the closing summary line.
    running_cost = 0.0
    cost_by_model: dict[str, float] = {}

    def process_one(batch: list[WorkItem]) -> list[DecisionRecord]:
        progress.batch_started()
        return _process_batch(client, batch, system_blocks, model, limiter, emit_locked, batch_seq)

    def on_complete(batch_records: list[DecisionRecord]) -> None:
        # Called from the main thread only (on_complete, per run_batches'
        # contract). Replaces the old per-batch tally print with the live
        # status line; non-tty gets a durable forced line per batch instead
        # (no in-place redraw on a redirected stream) so the classification
        # tally information isn't lost.
        nonlocal running_cost
        results.extend(batch_records)
        progress.batch_done(batch_records)
        snap = progress.snapshot()
        if console.is_tty():
            console.status(progress.render(), snapshot=snap)
        else:
            tally = Counter(
                r["decision"].get("classification", "error") for r in batch_records
            )
            parts = " / ".join(f"{count} {cls}" for cls, count in sorted(tally.items()))
            console.status(
                f"{progress.render()} | batch {progress.done_batches}: {parts}",
                force=True,
                snapshot=snap,
            )
        if batch_records:
            # Only the first record of a batch carries real usage (the rest
            # are zero_usage() — see contract/console.Progress docstrings),
            # and every record in a batch shares one model_version, so pricing
            # off batch_records[0] alone is exact, not an approximation.
            batch_model = batch_records[0].get("model_version") or model
            batch_usage = batch_records[0].get("usage") or {}
            batch_pricing = config.pricing_for(batch_model)
            batch_cost = (
                batch_usage.get("input_tokens", 0) / 1_000_000 * batch_pricing["input"]
                + batch_usage.get("output_tokens", 0) / 1_000_000 * batch_pricing["output"]
            )
            running_cost += batch_cost
            cost_by_model[batch_model] = cost_by_model.get(batch_model, 0.0) + batch_cost
        if cost_cap_usd is not None:
            # Raising here (main thread) unwinds run_batches, which cancels
            # queued batches.
            if running_cost > cost_cap_usd:
                raise SpendCapExceeded(
                    f"phase2 actual ${running_cost:.2f} > cap ${cost_cap_usd:.2f} "
                    f"({config.SPEND_CAP_MULTIPLIER}x forecast high)"
                )

    scheduling.run_batches(
        batches, process_one, max_workers=max_workers, on_complete=on_complete,
        interrupt_label="phase2",
    )

    console.clear_status()
    per_model = " (" + ", ".join(
        f"{m}: ${c:.4f}" for m, c in sorted(cost_by_model.items())
    ) + ")" if len(cost_by_model) > 1 else ""
    console.info(
        f"phase2 complete: {len(results)}/{total_rows} row(s) in {progress.done_batches}/"
        f"{n_batches} batch(es), cost ${running_cost:.4f}{per_model}"
    )

    return results
