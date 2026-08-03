"""deal_profile.py — Phase 1: the M&A deal-profile sweep.

M&A rows (acctnum MR58200000, config.MA_ACCTNUM) are auto-labeled
`non_recurring` by deterministic rule (basis "ma_account_rule") and are
FILTERED OUT of Phase-2 classification entirely — this module is now the
only place they are ever decided. They are also the sole input to the
sweep: a batched, invoice-reading pass that builds the quarter's deal
vocabulary (matter numbers, invoice numbers, property names, advisor names,
...) for later use as suspicion-only context in the Phase-2 system prompt
(see prompts.deal_profile_context_index / build_system_prompt).

`run_sweep` mirrors classify._process_batch's defensive split-and-retry
machinery, sharing scheduling.py's batching/token-estimation helpers rather
than re-implementing them: token-budgeted batches, vision rows isolated to
their own batch, a bounded per-request `timeout=` kwarg, and a per-row/
per-batch NEVER-RAISE contract — one bad row is flagged `deal_sweep_failed`
and the sweep continues, it never aborts on a bad row. Model routing mirrors
classify.py too: `_process_sweep_batch` resolves each batch's own model via
config.model_for_batch (a batch holding a readable invoice runs on
config.INVOICE_MODEL, everything else on the floor `model`), so a sweep can
mix both models and its cost is tracked per batch, not at one blended rate.
Every completed batch is followed by an incremental save of the profile JSON
(config.DEAL_PROFILE_JSON) and one DecisionRecord per row, emitted the
instant its (sub-)batch resolves, so a KeyboardInterrupt mid-sweep loses no
already-decided row. The model only ever returns forced-tool structured
output (BUILD_DEAL_PROFILE_TOOL); all validation, coercion, and
derived-field computation (quarters) is deterministic Python
here — the AI never writes a file.

The sweep runs concurrently through the same warmup + bounded-pool +
rate-limited scheduler as Phase 2 (scheduling.run_batches /
scheduling.build_limiter / scheduling.compute_max_workers) rather than a
sequential `for batch in batches` loop with no rate limiter — one shared
primitive, not a second copy. Because worker threads mutate the shared
entries/stats/usage accumulators directly, `run_sweep` itself is NOT
never-raise: a KeyboardInterrupt during the concurrent pool propagates out
of `run_batches` and out of `run_sweep` — callers must catch it (pipeline.py
does); whatever batches had already completed are durable via the
per-batch incremental save described above.

Quarter selection (which M&A rows feed a given sweep run) and the
forecast/paid-call confirmation gate are owned by cli.py/pipeline.py, not
this module; `sweep_forecast` here is pure ($0, no IO) so the caller can
print it before authorizing any spend.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, TypedDict

import anthropic

from gna_pipeline import config, console, prompts, scheduling
from gna_pipeline.contract import (
    DecisionRecord,
    RowPacket,
    Usage,
    WorkItem,
    invoice_summary_for_record,
    make_decision_record,
    vision_fallback_item,
    zero_usage,
)

logger = logging.getLogger("gna.deal_profile")

MA_ACCTNUM = config.MA_ACCTNUM

# Fixed per-batch system-prompt overhead used only for the sweep's own
# forecast: domain context + dealbuilder.md instructions, cached after batch
# 1 — charged once per batch at the high end (worst case, no cache reuse
# assumed), once total at the low end (best case, cache hit on every batch
# after the first).
SWEEP_SYSTEM_OVERHEAD = 1200


# ---------------------------------------------------------------------------
# Row selection
# ---------------------------------------------------------------------------

def select_ma_packets(packets: list[RowPacket]) -> list[RowPacket]:
    """All rows on the M&A acctnum."""
    return [p for p in packets if p.get("acctnum") == MA_ACCTNUM]


def select_ma_items(work_items: list[WorkItem]) -> list[WorkItem]:
    """WorkItems whose packet is on the M&A acctnum."""
    return [it for it in work_items if it["packet"].get("acctnum") == MA_ACCTNUM]


# ---------------------------------------------------------------------------
# Quarter helpers — period is YYYYMM.
# ---------------------------------------------------------------------------

def quarter_of(period: str) -> str | None:
    """"202603" -> "2026Q1". None for unparsable/empty input."""
    if not period or len(period) < 6:
        return None
    year_str, month_str = period[:4], period[4:6]
    if not (year_str.isdigit() and month_str.isdigit()):
        return None
    month = int(month_str)
    if not 1 <= month <= 12:
        return None
    quarter = (month - 1) // 3 + 1
    return f"{year_str}Q{quarter}"


_QUARTER_LABEL_RE = re.compile(r"^(\d{4})Q([1-4])$")


def months_of_quarter(label: str) -> list[str]:
    """"2026Q1" -> ["202601", "202602", "202603"]. Q1->01-03, Q2->04-06,
    Q3->07-09, Q4->10-12 -- the inverse of quarter_of at month granularity.
    Pure arithmetic, no IO. Raises ValueError on a malformed label (never
    guesses)."""
    m = _QUARTER_LABEL_RE.match(label.strip().upper())
    if not m:
        raise ValueError(f"invalid quarter label {label!r}; expected e.g. '2026Q1'")
    year_str, quarter = m.group(1), int(m.group(2))
    start_month = (quarter - 1) * 3 + 1
    return [f"{year_str}{month:02d}" for month in range(start_month, start_month + 3)]


def quarters_available(packets: list[RowPacket]) -> list[str]:
    """Sorted ascending distinct quarter labels over packets' period values
    (blank/unparsable periods ignored)."""
    quarters = {quarter_of(p.get("period") or "") for p in packets}
    quarters.discard(None)
    return sorted(quarters)  # type: ignore[arg-type]


def parse_quarters_arg(arg: str | None, available: list[str]) -> list[str]:
    """Resolve the CLI --quarters argument against the quarters actually
    present in the file.

    - None -> the latest available quarter.
    - all-digits "N" -> the latest N available quarters (ValueError if
      N < 1 or N > len(available)).
    - otherwise: comma-separated labels ("2025Q4,2026Q1"), case-insensitive
      (ValueError naming any label not in `available`).

    Every ValueError lists the quarters actually available. Returns sorted
    ascending.
    """
    if arg is None:
        if not available:
            raise ValueError(
                f"no quarters available to select a default from (available: {available})"
            )
        return [available[-1]]

    arg = arg.strip()
    if arg.isdigit():
        n = int(arg)
        if n < 1 or n > len(available):
            raise ValueError(
                f"--quarters {arg!r} requests the latest {n} quarter(s), but only "
                f"{len(available)} available: {available}"
            )
        return sorted(available[-n:])

    labels = [s.strip().upper() for s in arg.split(",") if s.strip()]
    invalid = [label for label in labels if label not in available]
    if invalid:
        raise ValueError(
            f"quarter label(s) not available: {invalid}; available quarters: {available}"
        )
    return sorted(set(labels))


def filter_items_by_quarters(
    items: list[WorkItem], quarters: list[str]
) -> tuple[list[WorkItem], list[WorkItem]]:
    """Split `items` into (selected, excluded) by quarter_of(packet.period)
    membership in `quarters`. An unparsable/blank period is excluded (the
    caller is responsible for warning about those row_idxs)."""
    quarter_set = set(quarters)
    selected: list[WorkItem] = []
    excluded: list[WorkItem] = []
    for item in items:
        q = quarter_of(item["packet"].get("period") or "")
        (selected if q is not None and q in quarter_set else excluded).append(item)
    return selected, excluded


# ---------------------------------------------------------------------------
# Optional human deal-context file
# ---------------------------------------------------------------------------

class DealContextTooLargeError(Exception):
    """workspace/user_deal_context.md exceeds config.DEAL_CONTEXT_WORD_CAP words.

    Deliberately fatal (cli.main catches it and exits, same pattern as
    CorruptProfileError) rather than silently truncating or proceeding — an
    oversized file gets sent on every batch of every paid call, so the
    operator must trim it, not have it silently clipped."""


_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def load_human_deals_md() -> str | None:
    """Load the single operator-authored deal-context file
    (config.DEAL_CONTEXT_MD, workspace/user_deal_context.md) as the higher-
    authority system block fed to BOTH the Phase-1 sweep and Phase-2
    classify prompts. The operator controls scope purely by what they write
    in the file for the current quarter(s) — there is no quarter-name
    convention.

    Falls back to config.DEAL_CONTEXT_MD_LEGACY (the pre-2026-07-17 filename
    workspace/deal_context.md) when DEAL_CONTEXT_MD is absent, so an existing
    quarter's notes under the old name are still read — with a one-line
    console.warn naming the new filename, so the operator renames it on disk
    and stops seeing the warning.

    HTML comments (`<!-- ... -->`) are stripped before anything else: the
    starter file ships with an instructional comment block (what to write,
    the word cap, a quarter-dating convention) so an operator opening it
    sees guidance, but that guidance is never sent to the model, and never
    counted against the word cap below -- it's authoring help, not deal
    content. Only what's left after stripping is judged/returned.

    Missing file, or empty/whitespace-only contents (after comment
    stripping) = silent no-op: return None so the caller omits the block.
    This is the common case (a fresh workspace/ or a quarter with no notes
    to add) so it does not print anything — never fabricate contents
    either way.

    Guards against an unbounded prompt: raises DealContextTooLargeError (a
    clear, operator-facing message with the actual word count) if the
    remaining content exceeds config.DEAL_CONTEXT_WORD_CAP words -- checked
    BEFORE any paid call (pipeline.py/cli.py load this ahead of the sweep).
    Past config.DEAL_CONTEXT_WORD_WARN it still loads, but prints a
    one-line heads-up so the operator notices before the file grows into a
    real problem.
    """
    path = config.DEAL_CONTEXT_MD
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        try:
            raw = config.DEAL_CONTEXT_MD_LEGACY.read_text(encoding="utf-8")
        except OSError:
            return None
        path = config.DEAL_CONTEXT_MD_LEGACY
        console.warn(
            f"reading deal context from the old filename {path} -- it has been "
            f"renamed to {config.DEAL_CONTEXT_MD.name}; rename the file on disk "
            f"to pick up the new name and stop seeing this warning"
        )

    text = _HTML_COMMENT_RE.sub("", raw).strip()
    if not text:
        return None

    word_count = len(text.split())
    if word_count > config.DEAL_CONTEXT_WORD_CAP:
        raise DealContextTooLargeError(
            f"{path} is {word_count} words, over the {config.DEAL_CONTEXT_WORD_CAP}-word "
            f"limit -- trim it before running (a huge deal-context file gets sent on "
            f"every batch of every paid call)"
        )
    if word_count > config.DEAL_CONTEXT_WORD_WARN:
        console.warn(
            f"{path.name} is {word_count} words (cap {config.DEAL_CONTEXT_WORD_CAP}) "
            f"-- consider trimming"
        )

    return text


# ---------------------------------------------------------------------------
# Profile persistence
# ---------------------------------------------------------------------------

def _empty_profile_shape() -> dict[str, Any]:
    return {
        "quarters": [],
        "period_range": "",
        "source_acctnum": MA_ACCTNUM,
        "entries": [],
    }


class CorruptProfileError(Exception):
    """quarter_deal_profile.json exists but could not be read or parsed.

    Deliberately fatal (cli.main catches it and exits): the old behavior
    treated a corrupt profile as "first run" and returned None, so the next
    sweep started from an empty accumulator and its FIRST incremental save
    overwrote the corrupt file — permanently destroying every previously
    accumulated deal's vocabulary, with only a log line as witness. A file
    that exists but doesn't parse must stop the run and make the operator
    decide (restore it, or delete the dealprofile folder to rebuild).
    """


def load_profile(path: str | Path) -> dict[str, Any] | None:
    """Read the profile JSON. None ONLY when the file is missing (a genuine
    first run). A file that exists but cannot be read or parsed raises
    CorruptProfileError — never None (see that class's docstring)."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise CorruptProfileError(
            f"could not read the deal profile at {p}: {exc}\n"
            f"  - if the file is corrupt (e.g. a crash mid-write), restore it from "
            f"a backup, or delete the folder {p.parent} to rebuild the profile "
            f"from scratch on the next deal-profile run\n"
            f"  - if it is open/locked in another program, close it and re-run"
        ) from exc


def save_profile(profile: dict[str, Any], path: str | Path) -> None:
    """Atomically write the profile JSON: temp file in the same directory,
    fsync, then os.replace (an atomic rename on Windows and POSIX alike).

    The sweep saves after EVERY batch, so the old truncate-then-write pattern
    opened a corruption window per batch — a crash/Ctrl+C mid-write left
    invalid JSON on disk. With the rename, the on-disk file is always either
    the complete previous save or the complete new one, never a mixture.
    """
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(profile, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, out_path)


def _merge_period_range(existing_range: str, periods: list[str]) -> str:
    candidates = [p for p in periods if p]
    if existing_range:
        candidates.extend(existing_range.split("-"))
    if not candidates:
        return existing_range
    return f"{min(candidates)}-{max(candidates)}"


# ---------------------------------------------------------------------------
# Forecast (pure, $0) — cli.py/pipeline.py prints/confirms.
# ---------------------------------------------------------------------------

def sweep_forecast(
    items: list[WorkItem], model: str, rate_limits: dict | None = None
) -> dict[str, Any]:
    """Pre-sweep cost/time estimate, mirroring classify.forecast's shape and
    math but sized for the sweep's own batching (config.DEAL_PROFILE_TARGET_TOKENS)
    and system-prompt overhead (SWEEP_SYSTEM_OVERHEAD).

    `rate_limits=None` (the default) still returns a wall-clock estimate —
    just the unmeasured-tier one (scheduling.compute_max_workers falls back to
    MAX_WORKERS_UNMEASURED)."""
    rows = len(items)

    # Two-tier split, mirroring classify.forecast: invoice-bearing rows run
    # (and price) at config.INVOICE_MODEL, the rest at the floor (`model`),
    # via the same per-row test config.model_for_batch applies per-batch.
    invoice_items: list[WorkItem] = []
    floor_items: list[WorkItem] = []
    for it in items:
        inv = it.get("invoice") or {}
        (invoice_items if inv.get("kind") in ("pdf", "text") else floor_items).append(it)
    rows_with_invoice = len(invoice_items)

    est_batches = len(scheduling.size_batches(items, config.DEAL_PROFILE_TARGET_TOKENS))

    input_tokens_high = (
        sum(it.get("est_input_tokens", 0) for it in items)
        + est_batches * SWEEP_SYSTEM_OVERHEAD
    )
    input_tokens_low = (
        sum(scheduling.low_end_tokens(it) for it in items) + SWEEP_SYSTEM_OVERHEAD
    )
    output_tokens_est = est_batches * 1500

    # The per-batch system overhead and the flat per-batch output estimate
    # aren't attributable to a single row the way classify.forecast's are (a
    # batch here mixes rows and, per config.model_for_batch, upgrades WHOLE
    # to INVOICE_MODEL if any row in it has a readable invoice) -- allocated
    # by each tier's share of rows, a reasonable approximation for a range
    # estimate that already says "ranges, not points" everywhere else.
    invoice_share = (rows_with_invoice / rows) if rows else 0.0
    floor_share = 1.0 - invoice_share

    pricing_invoice = config.pricing_for(config.INVOICE_MODEL)
    pricing_floor = config.pricing_for(model)

    invoice_input_low = (
        sum(scheduling.low_end_tokens(it) for it in invoice_items)
        + SWEEP_SYSTEM_OVERHEAD * invoice_share
    )
    invoice_input_high = (
        sum(it.get("est_input_tokens", 0) for it in invoice_items)
        + est_batches * SWEEP_SYSTEM_OVERHEAD * invoice_share
    )
    invoice_output = output_tokens_est * invoice_share

    floor_input_low = (
        sum(scheduling.low_end_tokens(it) for it in floor_items)
        + SWEEP_SYSTEM_OVERHEAD * floor_share
    )
    floor_input_high = (
        sum(it.get("est_input_tokens", 0) for it in floor_items)
        + est_batches * SWEEP_SYSTEM_OVERHEAD * floor_share
    )
    floor_output = output_tokens_est * floor_share

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

    # Concurrency-derived wall clock, floored against whichever of
    # RPM/ITPM/OTPM is actually measured.
    workers = scheduling.compute_max_workers(
        rate_limits,
        batch_target_tokens=config.DEAL_PROFILE_TARGET_TOKENS,
        est_output_per_batch=1500,
        latency_s=config.SCHED_SWEEP_LATENCY_S,
        n_batches=est_batches,
    )
    latency_s = config.SCHED_SWEEP_LATENCY_S
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
        "max_workers": workers,
        "wall_clock_est_min": wall_clock_est_min,
    }


class SweepStats(TypedDict):
    rows_selected: int
    rows_ok: int
    rows_failed: int
    invoices_read: int
    batches_total: int
    batches_failed: int
    entries: int


def _zero_stats() -> SweepStats:
    return SweepStats(
        rows_selected=0,
        rows_ok=0,
        rows_failed=0,
        invoices_read=0,
        batches_total=0,
        batches_failed=0,
        entries=0,
    )


# ---------------------------------------------------------------------------
# Entry coercion + cross-batch merge — deterministic Python, model output
# never trusted.
# ---------------------------------------------------------------------------

def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out = []
    for x in raw:
        s = str(x).strip()
        if s:
            out.append(s)
    return out


def _valid_row_idxs(raw: Any, allowed: set[int]) -> list[int]:
    if not isinstance(raw, list):
        return []
    out = []
    for x in raw:
        try:
            xi = int(x)
        except (TypeError, ValueError):
            continue
        if xi in allowed:
            out.append(xi)
    return out


def _union(a: list[Any], b: list[Any]) -> list[Any]:
    result = list(a)
    for x in b:
        if x not in result:
            result.append(x)
    return result


def _merge_entry(entries_acc: dict[str, dict[str, Any]], key: str, data: dict[str, Any]) -> None:
    """Fold one coerced entry (fresh from a batch) into the accumulator, keyed
    by name.lower(). Unions every list field; concatenates only novel
    evidence."""
    if key not in entries_acc:
        entries_acc[key] = {
            "name": data["name"],
            "aliases": list(data.get("aliases") or []),
            "matter_numbers": list(data.get("matter_numbers") or []),
            "invoice_numbers": list(data.get("invoice_numbers") or []),
            "properties": list(data.get("properties") or []),
            "entityids": list(data.get("entityids") or []),
            "advisors_seen": list(data.get("advisors_seen") or []),
            "evidence": list(data.get("evidence") or []),
            "supporting_row_idxs": list(data.get("supporting_row_idxs") or []),
        }
        return

    existing = entries_acc[key]
    existing["aliases"] = _union(existing["aliases"], data.get("aliases") or [])
    existing["matter_numbers"] = _union(existing["matter_numbers"], data.get("matter_numbers") or [])
    existing["invoice_numbers"] = _union(existing["invoice_numbers"], data.get("invoice_numbers") or [])
    existing["properties"] = _union(existing["properties"], data.get("properties") or [])
    existing["entityids"] = _union(existing["entityids"], data.get("entityids") or [])
    existing["advisors_seen"] = _union(existing["advisors_seen"], data.get("advisors_seen") or [])
    existing["evidence"] = existing["evidence"] + [
        e for e in (data.get("evidence") or []) if e not in existing["evidence"]
    ]
    existing["supporting_row_idxs"] = _union(
        existing["supporting_row_idxs"], data.get("supporting_row_idxs") or []
    )


def _coerce_and_merge_entry(
    entries_acc: dict[str, dict[str, Any]],
    raw_entry: Any,
    batch_row_idxs: set[int],
) -> None:
    """Validate + coerce one raw tool-output entry, then fold it into
    `entries_acc`. Drops entries with no evidence quotes or no name (printed,
    like the pre-rework file did)."""
    if not isinstance(raw_entry, dict):
        print(f"deal_profile: dropping malformed entry (not an object): {raw_entry!r}")
        return

    evidence = raw_entry.get("evidence") or []
    if not isinstance(evidence, list) or not evidence:
        print(
            f"deal_profile: dropping entry {raw_entry.get('name', '<unnamed>')!r} "
            "-- no evidence quotes"
        )
        return

    name = str(raw_entry.get("name", "")).strip()
    if not name:
        print("deal_profile: dropping entry with no name")
        return

    idxs = _valid_row_idxs(raw_entry.get("supporting_row_idxs"), batch_row_idxs)
    _merge_entry(
        entries_acc,
        name.lower(),
        {
            "name": name,
            "aliases": _str_list(raw_entry.get("aliases")),
            "matter_numbers": _str_list(raw_entry.get("matter_numbers")),
            "invoice_numbers": _str_list(raw_entry.get("invoice_numbers")),
            "properties": _str_list(raw_entry.get("properties")),
            "entityids": _str_list(raw_entry.get("entityids")),
            "advisors_seen": _str_list(raw_entry.get("advisors_seen")),
            "evidence": list(evidence),
            "supporting_row_idxs": idxs,
        },
    )


def _derive_entries(
    entries_acc: dict[str, dict[str, Any]], row_period_map: dict[int, str | None]
) -> list[dict[str, Any]]:
    """Recompute supporting_rows / quarters post-merge (never trust anything
    the model said about them)."""
    final_entries: list[dict[str, Any]] = []
    for data in entries_acc.values():
        idxs = sorted({int(i) for i in data["supporting_row_idxs"]})
        supporting_rows = len(idxs)
        quarters = set()
        for idx in idxs:
            q = quarter_of(row_period_map.get(idx) or "")
            if q:
                quarters.add(q)
        final_entries.append(
            {
                "name": data["name"],
                "aliases": data["aliases"],
                "matter_numbers": data["matter_numbers"],
                "invoice_numbers": data["invoice_numbers"],
                "properties": data["properties"],
                "entityids": data["entityids"],
                "advisors_seen": data["advisors_seen"],
                "evidence": data["evidence"],
                "supporting_row_idxs": idxs,
                "supporting_rows": supporting_rows,
                "quarters": sorted(quarters),
            }
        )
    return final_entries


_OK_REASONING = (
    "M&A account (MR58200000) row: non-recurring by rule; row swept for the "
    "quarter deal profile."
)
_FAILED_REASONING = _OK_REASONING + " sweep could not gather deal info (see flags)."


def _sweep_record(
    item: WorkItem, *, model: str | None, failed: bool, reason: str | None = None
) -> DecisionRecord:
    flags = list(item.get("flags") or [])
    reasoning = _OK_REASONING
    if failed:
        flags = flags + ["deal_sweep_failed"]
        # Surface the actual cause on the record (results.jsonl / audit tab), so
        # a genuine sweep failure is diagnosable instead of a bare flag. Only the
        # rows that fail even the text/row-only fallback reach here failed=True.
        reasoning = _FAILED_REASONING + (f" reason: {reason}" if reason else "")

    return make_decision_record(
        packet=item["packet"],
        row_hash=item["row_hash"],
        phase="deal_profile",
        classification="non_recurring",
        basis="ma_account_rule",
        reasoning=reasoning,
        evidence="M&A account rule",
        had_invoice=item["had_invoice"],
        invoice_accessed=item["invoice_accessed"],
        invoice=invoice_summary_for_record(item),
        model_version=None if failed else model,
        flags=flags,
    )


# ---------------------------------------------------------------------------
# Sweep driver — mirrors classify._process_batch's split-and-retry structure.
# ---------------------------------------------------------------------------

def _process_sweep_batch(
    client: Any,
    batch: list[WorkItem],
    system_blocks: list[dict[str, Any]],
    model: str,
    limiter: scheduling.RateLimiter,
    lock: threading.Lock,
    emit_locked: Callable[[DecisionRecord], None],
    entries_acc: dict[str, dict[str, Any]],
    stats: SweepStats,
    usage_total: Usage,
    cost_total: dict[str, float],
) -> bool:
    """Process one sweep batch. NEVER raises. Returns True if any row in this
    batch (or a split sub-batch) ended up flagged deal_sweep_failed.

    Concurrent-safe: `lock` guards every mutation of the shared entries_acc /
    stats / usage_total / cost_total accumulators, held only around those
    mutations — NEVER across the API call or an `emit_locked` call
    (emit_locked takes this same lock internally to serialize writes, and
    threading.Lock is not reentrant). `limiter.acquire` gates the request the
    same way classify._process_batch does.

    RequestTooLargeError / missing tool_use / a non-list entries payload ->
    split the batch in half and retry each half (each sub-batch re-acquires
    the limiter on its own — this falls out of the recursion for free),
    bottoming out at a 1-row batch that routes to fail_or_fallback. Any other
    exception -> fail_or_fallback for the whole batch.

    fail_or_fallback: a VISION batch (always single-row) that fails is NOT
    marked failed — its invoice is downgraded to text/row-only and the row is
    re-run ONCE on the floor model (vision_fallback_item), so it still
    contributes to the profile from the row's own information. Only a
    non-vision batch (including that downgraded re-run) marks its rows
    deal_sweep_failed, carrying the reason.
    """
    if not batch:
        return False

    # Per-batch model routing, identical rule to classify._process_batch:
    # `model` is the FLOOR the caller resolved; a batch holding any readable
    # invoice (kind pdf/text) upgrades to config.INVOICE_MODEL. Each split
    # sub-batch recomputes this on its own (a split can separate invoice rows
    # from floor rows).
    batch_model = config.model_for_batch(batch, model)

    vision = any((it.get("invoice") or {}).get("read_path") == "vision" for it in batch)
    timeout = config.SWEEP_API_TIMEOUT_VISION_S if vision else config.SWEEP_API_TIMEOUT_S

    def fail_or_fallback(reason: str) -> bool:
        """A vision batch (always a single row) that fails its image request
        does NOT fail the row: downgrade the invoice to text/row-only and re-run
        ONCE on the floor model, so the row still contributes to the profile
        from its own information. config.VISION_MAX_RETRIES is 0, so the image
        request itself never retried and never waited a second timeout. A
        non-vision batch — including that downgraded re-run — records the failure
        for real, carrying `reason` so it is diagnosable."""
        if vision:
            logger.warning(
                "deal_profile: sweep vision row(s)=%s failed (%s) -- retrying "
                "as text/row-only on the floor model",
                [it["packet"]["row_idx"] for it in batch], reason,
            )
            fallback = [vision_fallback_item(it, reason) for it in batch]
            return _process_sweep_batch(
                client, fallback, system_blocks, model, limiter, lock, emit_locked,
                entries_acc, stats, usage_total, cost_total,
            )
        for item in batch:
            logger.warning(
                "deal_profile: sweep row_idx=%d failed (%s) -- flagging deal_sweep_failed",
                item["packet"]["row_idx"], reason,
            )
            emit_locked(_sweep_record(item, model=None, failed=True, reason=reason))
        with lock:
            stats["rows_failed"] += len(batch)
        return True

    def split_and_retry(reason: str) -> bool:
        if len(batch) == 1:
            return fail_or_fallback(reason)
        logger.warning("deal_profile: sweep batch rows=%d split-retry (%s)", len(batch), reason)
        mid = len(batch) // 2
        left_failed = _process_sweep_batch(
            client, batch[:mid], system_blocks, model, limiter, lock, emit_locked,
            entries_acc, stats, usage_total, cost_total,
        )
        right_failed = _process_sweep_batch(
            client, batch[mid:], system_blocks, model, limiter, lock, emit_locked,
            entries_acc, stats, usage_total, cost_total,
        )
        return left_failed or right_failed

    est_tokens = sum(it.get("est_input_tokens", 0) for it in batch)
    limiter.acquire(est_tokens, config.MAX_TOKENS_DEAL_PROFILE)

    create_kwargs: dict[str, Any] = dict(
        model=batch_model,
        max_tokens=config.MAX_TOKENS_DEAL_PROFILE,
        system=system_blocks,
        messages=[{"role": "user", "content": prompts.build_deal_profile_user_content(batch)}],
        tools=[prompts.BUILD_DEAL_PROFILE_TOOL],
        tool_choice={"type": "tool", "name": "build_deal_profile"},
        timeout=timeout,
        # Same rationale as classify._make_request: Sonnet 5 (config.INVOICE_MODEL)
        # runs adaptive thinking by default, which would undermine the
        # deterministic forced-tool extraction this sweep depends on just as
        # much as Phase 2 does. A no-op on Sonnet 4.6.
        thinking={"type": "disabled"},
    )
    if config.supports_sampling_params(batch_model):
        create_kwargs["temperature"] = 0

    # Vision batches are single-row and a stuck PDF is rarely transient — cap
    # at VISION_MAX_RETRIES (0 = exactly one attempt, no second vision wait; a
    # failure falls back to text/row-only via fail_or_fallback). Request-scoped
    # name so the recursion in split_and_retry keeps the original client.
    req_client = (
        client.with_options(max_retries=config.VISION_MAX_RETRIES) if vision else client
    )

    try:
        resp = req_client.messages.create(**create_kwargs)
    except anthropic.RequestTooLargeError as exc:
        return split_and_retry(f"request_too_large: {exc}")
    except Exception as exc:  # noqa: BLE001 — never raise out of the sweep
        return fail_or_fallback(f"API call failed: {exc}")

    try:
        # Mirror classify._process_batch's truncation tripwire: a max_tokens
        # stop means the entries JSON was cut; a split halves the output each
        # half needs.
        if getattr(resp, "stop_reason", None) == "max_tokens":
            return split_and_retry(
                f"output truncated at max_tokens={config.MAX_TOKENS_DEAL_PROFILE}"
            )
        tool_block = next(
            (
                b for b in resp.content
                if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == "build_deal_profile"
            ),
            None,
        )
        if tool_block is None:
            return split_and_retry("no build_deal_profile tool_use block in response")

        payload = tool_block.input if isinstance(tool_block.input, dict) else None
        raw_entries = payload.get("entries") if payload else None
        if not isinstance(raw_entries, list):
            return split_and_retry("tool_use payload missing an entries array")

        batch_row_idxs = {it["packet"]["row_idx"] for it in batch}
        with lock:
            for raw_entry in raw_entries:
                _coerce_and_merge_entry(entries_acc, raw_entry, batch_row_idxs)

        usage = resp.usage
        for i, item in enumerate(batch):
            record = _sweep_record(item, model=batch_model, failed=False)
            if i == 0:
                record["usage"] = {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
                    "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
                    "usage_scope": f"sweep_batch_of_{len(batch)}",
                }
                with lock:
                    usage_total["input_tokens"] += usage.input_tokens
                    usage_total["output_tokens"] += usage.output_tokens
                    usage_total["cache_read_input_tokens"] += getattr(usage, "cache_read_input_tokens", 0) or 0
                    usage_total["cache_creation_input_tokens"] += (
                        getattr(usage, "cache_creation_input_tokens", 0) or 0
                    )
                    # Per-batch cost off THIS batch's own model, not one
                    # blended rate for the whole sweep -- a sweep can mix the
                    # floor and config.INVOICE_MODEL exactly like Phase 2.
                    batch_pricing = config.pricing_for(batch_model)
                    batch_cost = (
                        usage.input_tokens / 1_000_000 * batch_pricing["input"]
                        + usage.output_tokens / 1_000_000 * batch_pricing["output"]
                    )
                    cost_total["total"] = cost_total.get("total", 0.0) + batch_cost
                    cost_total[batch_model] = cost_total.get(batch_model, 0.0) + batch_cost
            else:
                record["usage"] = zero_usage()
            emit_locked(record)
            with lock:
                stats["rows_ok"] += 1
        return False
    except Exception as exc:  # noqa: BLE001 — never raise out of the sweep
        return fail_or_fallback(f"response parsing failed: {exc}")


def run_sweep(
    client: Any,
    items: list[WorkItem],
    *,
    model: str,
    human_deals_md: str | None,
    rate_limits: dict | None,
    cost_cap_usd: float | None = None,
    emit: Callable[[DecisionRecord], None],
) -> tuple[dict[str, Any], SweepStats, Usage]:
    """Run the batched deal-profile sweep over `items` (already filtered to
    the M&A rows in scope for this run's selected quarters).

    Per-row/per-batch processing never raises (see _process_sweep_batch's
    split-and-retry), but this function itself CAN raise: a KeyboardInterrupt
    during the concurrent pool (scheduling.run_batches) propagates straight
    out — callers must catch it (cli.py/pipeline.py does, on both the
    standalone `deal-profile` command and `run`) — and so does
    scheduling.SpendCapExceeded when `cost_cap_usd` is set and the
    accumulated actual cost crosses it
    (checked per completed batch; same salvage semantics as an interrupt).
    Whatever batches had already completed before the interrupt are durable:
    the profile is saved after every batch (config.DEAL_PROFILE_JSON) and
    each row's DecisionRecord is emitted the instant its (sub-)batch
    resolves — see module docstring.
    """
    usage_total: Usage = zero_usage()
    # Per-batch cost, keyed by model (plus a "total" key) -- a sweep can mix
    # the floor and config.INVOICE_MODEL exactly like Phase 2 (see
    # config.model_for_batch), so this is NOT usage_total priced at one
    # blended rate. Kept internal to this function (mirrors
    # classify.run_classification's running_cost); not part of the returned
    # tuple, same as usage_total's own accounting role.
    cost_total: dict[str, float] = {}

    if not items:
        return _empty_profile_shape(), _zero_stats(), usage_total

    stats: SweepStats = SweepStats(
        rows_selected=len(items),
        rows_ok=0,
        rows_failed=0,
        invoices_read=sum(1 for it in items if it.get("invoice_accessed") == "yes"),
        batches_total=0,
        batches_failed=0,
        entries=0,
    )

    row_period_map: dict[int, str | None] = {
        it["packet"]["row_idx"]: it["packet"].get("period") for it in items
    }

    entries_acc: dict[str, dict[str, Any]] = {}

    quarters_swept = {
        q for q in (quarter_of(p or "") for p in row_period_map.values()) if q
    }
    period_range = _merge_period_range(
        "",
        [p for p in row_period_map.values() if p],
    )

    system_blocks = prompts.build_sweep_system_prompt(human_deals_md)
    batches = scheduling.size_batches(items, config.DEAL_PROFILE_TARGET_TOKENS)
    stats["batches_total"] = len(batches)

    limiter = scheduling.build_limiter(rate_limits)
    workers = scheduling.compute_max_workers(
        rate_limits,
        batch_target_tokens=config.DEAL_PROFILE_TARGET_TOKENS,
        est_output_per_batch=config.MAX_TOKENS_DEAL_PROFILE,
        latency_s=config.SCHED_SWEEP_LATENCY_S,
        n_batches=len(batches),
    )

    # ONE shared lock guards every worker-side mutation of entries_acc /
    # stats / usage_total (inside _process_sweep_batch) AND every emit call
    # (emit_locked) -- never both at once (threading.Lock is not reentrant;
    # emit_locked is always called OUTSIDE any `with lock:` block).
    lock = threading.Lock()

    def emit_locked(record: DecisionRecord) -> None:
        with lock:
            emit(record)

    console.kv([
        ("rows", str(len(items))),
        ("batches", str(len(batches))),
        ("max_workers", f"{workers} (derived; ceiling {config.MAX_WORKERS_CEILING})"),
        ("warmup", "batch 1 runs alone to warm the prompt cache"),
    ])

    progress = console.Progress(
        total_rows=len(items), total_batches=len(batches), model=model, unit="swept"
    )

    profile: dict[str, Any] = {
        "quarters": sorted(quarters_swept),
        "period_range": period_range,
        "source_acctnum": MA_ACCTNUM,
        "entries": [],
    }

    def process_one(batch: list[WorkItem]) -> bool:
        progress.batch_started()
        return _process_sweep_batch(
            client, batch, system_blocks, model, limiter, lock, emit_locked,
            entries_acc, stats, usage_total, cost_total,
        )

    def on_complete(batch_failed: bool) -> None:
        nonlocal profile
        if batch_failed:
            stats["batches_failed"] += 1
        with lock:
            entries = _derive_entries(entries_acc, row_period_map)
            done_rows_snapshot = stats["rows_ok"] + stats["rows_failed"]
            usage_snapshot = dict(usage_total)
            running_cost = cost_total.get("total", 0.0)
        profile = {
            "quarters": sorted(quarters_swept),
            "period_range": period_range,
            "source_acctnum": MA_ACCTNUM,
            "entries": entries,
        }
        save_profile(profile, config.DEAL_PROFILE_JSON)

        # batch_done's counters are absolute snapshots here (not an
        # increment from records), since this on_complete already holds the
        # authoritative shared accumulators -- see console.Progress docstring.
        progress.batch_done(done_rows=done_rows_snapshot, usage=usage_snapshot, cost=running_cost)
        snap = progress.snapshot()
        if console.is_tty():
            console.status(progress.render(), snapshot=snap)
        else:
            console.status(
                f"{progress.render()} | batch {progress.done_batches}: "
                f"{'failed' if batch_failed else 'ok'}",
                force=True,
                snapshot=snap,
            )
        if cost_cap_usd is not None:
            # Runtime spend rail — running_cost is cost_total's own per-batch,
            # per-model accounting (NOT usage_snapshot priced at one blended
            # rate: a sweep can mix the floor and config.INVOICE_MODEL).
            # Raising here (main thread) unwinds scheduling.run_batches,
            # which cancels queued batches; the profile save above already
            # ran, so everything swept so far is durable.
            if running_cost > cost_cap_usd:
                raise scheduling.SpendCapExceeded(
                    f"phase1 sweep actual ${running_cost:.2f} > cap ${cost_cap_usd:.2f} "
                    f"({config.SPEND_CAP_MULTIPLIER}x forecast high)"
                )

    scheduling.run_batches(
        batches, process_one, max_workers=workers, on_complete=on_complete,
        interrupt_label="phase1 sweep",
    )

    console.clear_status()
    cost = cost_total.get("total", 0.0)
    per_model = " (" + ", ".join(
        f"{m}: ${c:.4f}" for m, c in sorted(cost_total.items()) if m != "total"
    ) + ")" if len(cost_total) > 2 else ""
    stats["entries"] = len(profile["entries"])
    console.info(
        f"phase1 sweep complete: {stats['rows_ok']}/{stats['rows_selected']} row(s) swept ok "
        f"({stats['rows_failed']} failed) in {stats['batches_total']} batch(es), "
        f"cost ${cost:.4f}{per_model}"
    )
    return profile, stats, usage_total
