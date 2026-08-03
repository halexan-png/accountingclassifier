"""pipeline.py — the full `run` pipeline (Phase 0 -> 1 -> 2 -> 3), plus the
small operational helpers shared with `deal-profile`/`recover` in cli.py.

`run_pipeline` is the readable spine cli.py's `cmd_run` delegates to: ingest +
scope -> quarter selection -> invoice lookup index -> Phase 0 (prep +
invoices) -> partition (M&A vs. classify) -> forecast -> Phase 1 (deal sweep)
-> Phase 2 (classify) -> Phase 3 (summary + Excel). Each stage is a named
function below, in that order, holding cmd_run's original logic verbatim;
cli.py owns no business logic of its own — it only maps an
argparse.Namespace onto this function's keyword arguments.

There is no resume/reuse path anywhere in this spine: every `run` always
reclassifies every in-scope row and always re-sweeps the quarter's M&A rows.
There is also no cross-run persistence of the deal profile: each run's sweep
rebuilds the quarter's deal vocabulary from scratch, from that quarter's own
M&A rows only, and the saved quarter_deal_profile.json is overwritten with
the current run's profile — never folded together with a prior one.

Deliberately decoupled from argparse: every parameter here is a plain value,
so the pipeline is callable (and testable) without building a Namespace.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import anthropic

from gna_pipeline import (
    classify,
    config,
    console,
    deal_profile,
    excel_out,
    ingest,
    invoice_mining,
    persistence,
    prep,
    prompts,
    reporting,
    scheduling,
)
from gna_pipeline.contract import (
    DecisionRecord,
    WorkItem,
    invoice_summary_for_record,
    make_decision_record,
)

logger = logging.getLogger("gna.pipeline")


# ---------------------------------------------------------------------------
# Shared operational helpers — used by `run` (below) and by cli.py's
# `deal-profile` / `probe-limits` / `recover` commands, which stay in cli.py
# but are thin enough that duplicating these would be the only alternative.
# ---------------------------------------------------------------------------

def build_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(max_retries=config.API_MAX_RETRIES, timeout=config.API_TIMEOUT_S)


def need_api_key() -> bool:
    if config.api_key_present():
        return False
    print(
        "ERROR: ANTHROPIC_API_KEY is not set. Add it to .env "
        "(ANTHROPIC_API_KEY=sk-ant-...) or the environment before running this command.",
        file=sys.stderr,
    )
    return True


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic JSON write (temp + os.replace), same rationale as
    deal_profile.save_profile: never leave a half-written file on crash."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True, default=str)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)


def measure_rate_limits(client: anthropic.Anthropic, model: str) -> dict[str, Any]:
    """1-token ping against `model`; parse the anthropic-ratelimit-* headers,
    write rules/rate_limits.json, return the payload. Raises on API failure —
    callers decide whether that's fatal (`probe-limits`) or a warn-and-fall-
    back (`refresh_rate_limits`)."""
    raw = client.messages.with_raw_response.create(
        model=model,
        max_tokens=1,
        messages=[{"role": "user", "content": "ping"}],
    )
    headers = raw.headers

    def _int_header(name: str) -> int | None:
        val = headers.get(name)
        try:
            return int(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    payload = {
        "measured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "model": model,
        "requests_limit": _int_header("anthropic-ratelimit-requests-limit"),
        "input_tokens_limit": _int_header("anthropic-ratelimit-input-tokens-limit"),
        "output_tokens_limit": _int_header("anthropic-ratelimit-output-tokens-limit"),
    }
    write_json(config.RATE_LIMITS_JSON, payload)
    return payload


def refresh_rate_limits(
    client: anthropic.Anthropic, model: str, stale: dict | None
) -> dict | None:
    """Run-start probe: re-measure limits for THE model this run actually
    uses, so rules/rate_limits.json never governs a run with numbers measured
    long ago or for a different model. Costs ~1 token. Returns `stale`
    unchanged (warn, never fatal) if the probe fails — the run then proceeds
    on the previously measured limits, exactly as before."""
    try:
        fresh = measure_rate_limits(client, model)
    except Exception as exc:  # noqa: BLE001 — a probe failure must not kill the run
        console.warn(
            f"rate-limit probe failed ({exc}) -- proceeding on previously "
            f"measured limits"
        )
        return stale
    console.info(
        f"rate limits re-measured for {model}: rpm={fresh.get('requests_limit')} "
        f"itpm={fresh.get('input_tokens_limit')} otpm={fresh.get('output_tokens_limit')}"
    )
    return fresh


def write_context_txt(index_text: str) -> None:
    config.DEAL_PROFILE_CONTEXT_TXT.parent.mkdir(parents=True, exist_ok=True)
    config.DEAL_PROFILE_CONTEXT_TXT.write_text(index_text, encoding="utf-8")


def print_context_report(report: dict[str, Any]) -> None:
    """One info line for the known-deal index size. The index is uncapped now
    (prompts.deal_profile_context_index emits every deal's full line), so
    nothing is ever collapsed or dropped — the whole index ships, cheap once
    the system prompt is cached. Uses .get so an older report shape still
    prints without a KeyError."""
    console.info(
        f"context: known-deal index ~{report.get('est_tokens', 0)} tok, "
        f"{report.get('entries_full', 0)} deal(s) (full, no cap)"
    )
    console.data("context_report", report)


def empty_deal_profile() -> dict[str, Any]:
    """The Phase-1 profile shape with no entries -- used when there is
    nothing to sweep and no saved profile to fall back to."""
    return {"quarters": [], "period_range": "", "source_acctnum": config.MA_ACCTNUM, "entries": []}


def print_phase0_stats(stats0: prep.Phase0Stats) -> None:
    console.kv([
        ("reclass_fired", str(stats0["reclass_fired"])),
        ("closegl_fired", str(stats0["closegl_fired"])),
        ("negatives_skipped", str(stats0["negatives_skipped"])),
    ])
    console.kv([
        ("had_invoice_yes", str(stats0["had_invoice_yes"])),
        ("invoice_accessed_yes", str(stats0["invoice_accessed_yes"])),
        ("invoice_unavailable", str(stats0["invoice_unavailable"])),
        ("invoice_read_failed", str(stats0["invoice_read_failed"])),
        ("url_fetched_ok", str(stats0["url_fetched_ok"])),
        ("url_fetch_failed", str(stats0["url_fetch_failed"])),
        ("local_resolved", str(stats0["local_resolved"])),
        ("local_no_match", str(stats0["local_no_match"])),
        ("ambiguous", str(stats0["ambiguous"])),
        ("errors", str(stats0["errors"])),
    ], indent=4)
    if stats0["closegl_guard_trips"]:
        console.warn(
            f"!! CLOSEGL GUARD TRIPPED {stats0['closegl_guard_trips']} time(s) -- these rows "
            f"were routed to human_review instead of auto-clearing, review them !!"
        )
    # Emit only the scalar counts to the UI event stream -- NOT stats0's
    # internal `work_items` list, whose invoice results can carry raw PDF
    # `pdf_bytes` (not JSON-serializable, and megabytes the browser never
    # needs). stats0 itself is untouched; the pipeline still uses work_items.
    console.data("phase0_stats", {k: v for k, v in stats0.items() if k != "work_items"})


def invoice_lookup_banner_lines(stats0: prep.Phase0Stats) -> list[str]:
    """Fail-loud heads-up on local invoice-lookup availability: read-only
    reporting of what Phase 0 (prep.prepare_rows) already did -- never
    changes resolution/matching behavior itself.

    "OFF" means the local lookup directory/CSV this run (config.INVOICE_DIR,
    config.INVOICE_LOOKUP_CSV) is missing or empty, so every row that mines
    an invoice key falls through to invoice_unavailable no matter how good
    the key is. When it's present, "N of M resolved" scopes M to LOCAL
    resolution attempts only (local_resolved + local_no_match + ambiguous --
    rows with no URL that mined a key and tried the local directory); a
    URL-sourced invoice never touches this directory at all, so it is
    deliberately excluded from both N and M."""
    dir_ready = config.INVOICE_DIR.is_dir() and any(config.INVOICE_DIR.glob("*.pdf"))
    csv_ready = config.INVOICE_LOOKUP_CSV.is_file()
    if not (dir_ready and csv_ready):
        return [
            "LOCAL INVOICE LOOKUP IS OFF",
            f"  missing/empty: {config.INVOICE_DIR} or {config.INVOICE_LOOKUP_CSV}",
            "  every mined invoice key will resolve as invoice_unavailable this run",
        ]
    attempted = stats0["local_resolved"] + stats0["local_no_match"] + stats0["ambiguous"]
    return [f"Local invoices: {stats0['local_resolved']} of {attempted} resolved"]


def _tier_line(forecast: dict[str, Any]) -> str | None:
    """One line summarizing classify.forecast's / deal_profile.sweep_forecast's
    `cost_by_tier` split (floor model vs config.INVOICE_MODEL) -- None if the
    forecast dict doesn't carry it (defensive; an older/foreign forecast
    still prints without a KeyError)."""
    tier = forecast.get("cost_by_tier")
    if not tier:
        return None
    floor, invoice = tier.get("floor") or {}, tier.get("invoice") or {}
    return (
        f"floor rows: {floor.get('rows', 0)} @ {floor.get('model', '?')} "
        f"(${floor.get('cost_low_usd', 0):.2f}-${floor.get('cost_high_usd', 0):.2f})  --  "
        f"invoice rows: {invoice.get('rows', 0)} @ {invoice.get('model', '?')} "
        f"(${invoice.get('cost_low_usd', 0):.2f}-${invoice.get('cost_high_usd', 0):.2f})"
    )


def print_phase1_forecast(forecast: dict[str, Any]) -> None:
    """Informational Phase-1 cost line (mirrors classify.forecast's line
    shape), from deal_profile.sweep_forecast — pure, $0, so a --n sample
    forecasts its own small cost, not the full-quarter constant."""
    pairs: list[tuple[str, str]] = [
        ("rows", str(forecast["rows"])),
        ("est. calls", f"~{forecast['est_batches']}"),
        (
            "est. input",
            f"{forecast['input_tokens_low'] / 1000:.1f}k-"
            f"{forecast['input_tokens_high'] / 1000:.1f}k tok",
        ),
        ("est. cost", f"${forecast['cost_low_usd']:.2f}-${forecast['cost_high_usd']:.2f}"),
    ]
    tier_line = _tier_line(forecast)
    if tier_line:
        pairs.append(("by tier", tier_line))
    pairs.extend([
        (
            "spend rail",
            f"aborts past ${config.SPEND_CAP_MULTIPLIER * forecast['cost_high_usd']:.2f} "
            f"({config.SPEND_CAP_MULTIPLIER}x high)",
        ),
        ("max_workers", f"{forecast['max_workers']} (derived)"),
        ("est. wall clock", f"~{forecast['wall_clock_est_min']:.1f} min"),
    ])
    console.kv(pairs)
    console.data("sweep_forecast", forecast)


def _print_phase2_forecast(forecast: dict[str, Any], rate_limits: dict | None) -> None:
    """Print the mandatory pre-run forecast block, ranges not points."""
    rpm = rate_limits.get("requests_limit") if rate_limits else None
    itpm = rate_limits.get("input_tokens_limit") if rate_limits else None
    limits_str = f"{rpm if rpm else 'unmeasured'}/{itpm if itpm else 'unmeasured'}"

    pairs: list[tuple[str, str]] = [
        ("rows", str(forecast["rows"])),
        ("with resolved invoices", str(forecast["rows_with_invoice"])),
        ("est. batches", f"~{forecast['est_batches']}"),
        (
            "est. input",
            f"{forecast['input_tokens_low'] / 1_000_000:.2f}M-"
            f"{forecast['input_tokens_high'] / 1_000_000:.2f}M tok",
        ),
        ("est. output", f"{forecast['output_tokens_est'] / 1_000_000:.2f}M tok"),
        (
            "TOTAL ESTIMATE",
            f"${forecast['cost_low_usd']:.2f}-${forecast['cost_high_usd']:.2f} (+cache)",
        ),
    ]
    tier_line = _tier_line(forecast)
    if tier_line:
        pairs.append(("by tier", tier_line))
    pairs.extend([
        (
            "spend rail",
            f"aborts past ${config.SPEND_CAP_MULTIPLIER * forecast['cost_high_usd']:.2f} "
            f"({config.SPEND_CAP_MULTIPLIER}x high)",
        ),
        (
            "measured limits",
            f"{limits_str} -> max_workers={forecast['max_workers']}, "
            f"est. wall clock ~{forecast['wall_clock_est_min']:.1f} min",
        ),
    ])
    console.kv(pairs)
    console.data("classify_forecast", forecast)


def print_closing_tally(
    summary: dict[str, Any], actual_wall_clock_min: float | None = None
) -> None:
    pairs: list[tuple[str, str]] = [
        (f"  {cls}", str(count))
        for cls, count in sorted(summary["tally"]["by_classification"].items())
    ]
    dp = summary.get("deal_profile")
    if dp:
        pairs.append((
            "deal profile",
            f"{dp.get('entry_count', 0)} entr(ies); M&A rows: {dp.get('swept_ok', 0)} swept "
            f"ok / {dp.get('sweep_failed', 0)} failed / {dp.get('sweep_skipped', 0)} not "
            f"swept; {dp.get('invoices_read', 0)} invoice(s) read",
        ))
    usage = summary["usage"]
    pairs.append(("cost actual", f"${usage['cost_actual_usd']:.4f}"))
    forecast = summary.get("forecast")
    if forecast and "actual_cost_usd" in forecast:
        pairs.append((
            "forecast was",
            f"${forecast.get('cost_low_usd', 0):.2f}-${forecast.get('cost_high_usd', 0):.2f}  "
            f"delta: ${forecast['delta_usd']:.4f}",
        ))
    if actual_wall_clock_min is not None:
        # Calibration signal for config.SCHED_CLASSIFY_LATENCY_S /
        # SCHED_SWEEP_LATENCY_S -- compare against the pre-run forecast's
        # "est. wall clock" line above.
        pairs.append(("actual wall clock", f"{actual_wall_clock_min:.1f} min"))
    console.kv(pairs)
    console.data("closing_tally", summary)


def print_scope_line(prefix: str, scope_stats: dict[str, Any]) -> None:
    """One-line summary of ingest.filter_scope's outcome, shared by `run` and
    `deal-profile` (both apply the same --months/--min-usd window)."""
    if scope_stats.get("months_arg") == "all" and scope_stats.get("min_usd") == 0:
        console.info(f"{prefix}: scope -- full file")
        return
    periods_selected = scope_stats.get("periods_selected") or []
    periods_all = scope_stats.get("periods_all") or []
    period_range = f"{min(periods_selected)}..{max(periods_selected)}" if periods_selected else "none"
    console.info(
        f"{prefix}: scope -- periods {period_range} "
        f"({len(periods_selected)} of {len(periods_all)} in file), "
        f"|USD| >= {scope_stats.get('min_usd', 0):g}: kept {scope_stats.get('kept')} of "
        f"{scope_stats.get('total_in')} (excluded {scope_stats.get('excluded_by_period')} by period, "
        f"{scope_stats.get('excluded_by_amount')} by amount)"
    )


def _short(value: Any, width: int = 48) -> str:
    text = str(value) if value not in (None, "") else "-"
    return text if len(text) <= width else text[: width - 3] + "..."


def _parse_rows_arg(rows: str) -> set[int]:
    """'63' or '63,64,70' -> {63, 64, 70}. Raises ValueError on junk."""
    parsed = {int(part) for part in str(rows).split(",") if part.strip()}
    if not parsed:
        raise ValueError(f"--rows {rows!r} names no rows")
    return parsed


def _results_path_for(record: DecisionRecord) -> Path:
    """Stage-scoped store routing: every M&A-account record — sweep, skip, or
    the M&A row's phase0 resolution — belongs to the deal-profile stage's own
    results.jsonl (config.DEAL_RESULTS_JSONL); everything else to the
    classification stage's (config.RESULTS_JSONL). Each stage's audit history
    lives in its own file (never consulted to skip work — every run re-decides
    every row), so deleting a stage's folder cleanly clears exactly that
    stage's history and nothing else."""
    if record["packet"].get("acctnum") == config.MA_ACCTNUM:
        return config.DEAL_RESULTS_JSONL
    return config.RESULTS_JSONL


# ---------------------------------------------------------------------------
# deal_sweep_skipped record assembly (`run`'s job, not deal_profile's — a
# full run owns the labeling of M&A rows it chose not to sweep this time).
# ---------------------------------------------------------------------------

def _quarter_skip_reasoning(quarters: list[str]) -> str:
    return (
        "M&A account (MR58200000) row: non-recurring by rule; not swept this run "
        f"(outside selected sweep quarter(s) {', '.join(quarters)})."
    )


def _deal_sweep_skipped_record(item: WorkItem, *, reasoning: str) -> DecisionRecord:
    """Auto non_recurring record for an M&A WorkItem this run chose NOT to
    sweep (outside selected quarters).
    Mirrors deal_profile._sweep_record's shape; reuses the shared
    invoice-summary rule so the invoice column matches what the sweep itself
    would have written."""
    flags = list(item.get("flags") or []) + ["deal_sweep_skipped"]
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
        model_version=None,
        flags=flags,
    )


# ---------------------------------------------------------------------------
# Stage 1 — ingest + classification scope + sampling scope.
# ---------------------------------------------------------------------------

def _stage1_ingest_and_scope(
    workbook: Path,
    months: str | None,
    min_usd: float | None,
    row_mode: bool,
    sample_mode: bool,
    wanted_rows: set[int],
    n: int | None,
) -> tuple[list, dict[str, Any]] | None:
    """Full run: every non-M&A row IN SCOPE flows to classification; every
    M&A row in scope feeds the Phase-1 sweep instead (never classified --
    auto non_recurring by rule). Sample run (--n): n//2 M&A rows feed the
    sweep, and the remaining n - n//2 rows come from the non-M&A pool -- a
    cheap end-to-end rehearsal of both phases. Row mode (--rows): classifies
    exactly the named rows, M&A or not, with no sweep at all (the saved
    profile, if any, is reused as-is) and BYPASSES the --months/--min-usd
    scope filters entirely -- it is an explicit, already-scoped row selection
    over the full file.

    Returns (packets, scope_stats), or None on a scope error (--rows naming
    nothing, or an invalid --months/--min-usd value) -- the caller returns
    exit code 2.
    """
    console.section("Ingest")
    all_packets, stats = ingest.read_packets(str(workbook), sheet=config.SHEET_NAME)
    console.info(f"ingested {len(all_packets)} row(s) from {workbook}")

    if row_mode:
        packets_in_scope = all_packets
        scope_stats: dict[str, Any] = {"mode": "rows", "rows": sorted(wanted_rows)}
        console.info("--rows mode -- --months/--min-usd scope filters bypassed (explicit row selection)")
    else:
        try:
            packets_in_scope, scope_stats = ingest.filter_scope(all_packets, months, min_usd)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return None
        print_scope_line("run", scope_stats)

    if row_mode:
        packets = [p for p in packets_in_scope if p["row_idx"] in wanted_rows]
        missing = sorted(wanted_rows - {p["row_idx"] for p in packets})
        console.info(f"--rows selected {len(packets)} row(s): {sorted(wanted_rows)}")
        if missing:
            console.warn(
                f"row(s) {missing} not found in the ingested data (header/blank rows are "
                f"skipped; data rows are Excel rows 2.."
                f"{max((p['row_idx'] for p in all_packets), default=1)})"
            )
        if not packets:
            print("ERROR: --rows matched nothing; nothing to do.", file=sys.stderr)
            return None
        for p in packets:
            console.info(
                f"selected row: row_idx={p['row_idx']} "
                f"acctnum={p.get('acctnum')} amount={p.get('amount')} "
                f"category={_short(p.get('category'))} descrptn={_short(p.get('descrptn'))}"
            )
    elif sample_mode:
        sample_n = max(n, 0)
        ma_share = sample_n // 2
        ma_all = deal_profile.select_ma_packets(packets_in_scope)
        ma_sample = ma_all[:ma_share]
        non_ma = [p for p in packets_in_scope if p.get("acctnum") != config.MA_ACCTNUM]
        non_ma_sample = non_ma[: sample_n - len(ma_sample)]
        packets = ma_sample + non_ma_sample
        console.info(f"{len(ma_all)} row(s) in scope on M&A acctnum {config.MA_ACCTNUM}")
        console.info(
            f"sample --n {sample_n} -> {len(ma_sample)} M&A row(s) feed the deal-profile sweep, "
            f"{len(non_ma_sample)} non-M&A row(s) flow to classification"
        )
        if len(ma_all) < ma_share:
            console.warn(
                f"only {len(ma_all)} M&A row(s) in scope (< {ma_share} requested); "
                f"remainder given to the classification sample"
            )
        for p in ma_sample:
            console.info(
                f"sample deal-profile row: row_idx={p['row_idx']} "
                f"acctnum={p.get('acctnum')} category={_short(p.get('category'))} "
                f"descrptn={_short(p.get('descrptn'))}"
            )
        for p in non_ma_sample:
            console.info(
                f"sample classification row: row_idx={p['row_idx']} "
                f"acctnum={p.get('acctnum')} category={_short(p.get('category'))} "
                f"descrptn={_short(p.get('descrptn'))}"
            )
    else:
        packets = packets_in_scope
        console.info(f"full run over {len(packets)} in-scope row(s)")

    if stats["column_warnings"]:
        console.warn(
            f"{len(stats['column_warnings'])} column-resolution warning(s); "
            f"run ingest-check for detail"
        )

    return packets, scope_stats


# ---------------------------------------------------------------------------
# Stage 2 — quarter selection.
# ---------------------------------------------------------------------------

def _stage2_select_quarters(
    row_mode: bool, packets: list, quarters_arg: str | None,
) -> tuple[list[str], list] | None:
    """Parsed EARLY (before invoice lookup / prep / any URL fetches) so a
    --quarters typo costs nothing. Row mode never sweeps, so it skips this
    entirely. Returns (quarters, ma_scope_packets), or None on an invalid
    --quarters value -- the caller returns exit code 2."""
    if row_mode:
        return [], []

    ma_scope_packets = deal_profile.select_ma_packets(packets)
    if not ma_scope_packets:
        console.warn("no M&A row(s) in scope; skipping the deal-profile sweep")
        return [], ma_scope_packets

    available = deal_profile.quarters_available(ma_scope_packets)
    try:
        quarters = deal_profile.parse_quarters_arg(quarters_arg, available)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return None
    return quarters, ma_scope_packets


# ---------------------------------------------------------------------------
# Stage 4 — invoice lookup index.
# ---------------------------------------------------------------------------

def _stage4_load_lookup_index() -> dict:
    try:
        return invoice_mining.load_lookup_index(config.INVOICE_LOOKUP_CSV)
    except OSError as exc:
        logger.warning(
            "run: could not load invoice lookup index at %s (%s); proceeding with an empty index",
            config.INVOICE_LOOKUP_CSV, exc,
        )
        return {}


# ---------------------------------------------------------------------------
# Stage 5 — Phase 0: deterministic prep + invoice resolution.
# ---------------------------------------------------------------------------

def _stage5_phase0(
    packets: list,
    lookup_index: dict,
    dry_run: bool,
    sample_mode: bool,
    row_mode: bool,
    no_fetch: bool,
) -> tuple[list[DecisionRecord], prep.Phase0Stats, list[WorkItem], dict | None]:
    """Runs over the whole in-scope packet set, including M&A sample rows in
    --n mode, so their invoices resolve for the sweep. Returns
    (phase0_records, stats0, work_items, rate_limits)."""
    console.section("Phase 0: prep + invoices")
    phase0_records: list[DecisionRecord] = []
    if dry_run:
        def emit0(record: DecisionRecord) -> None:  # no-op -- nothing spent yet, nothing to keep
            return None
    else:
        def emit0(record: DecisionRecord) -> None:
            persistence.append_record(_results_path_for(record), record)
            phase0_records.append(record)
            if sample_mode or row_mode:
                decision = record["decision"]
                console.info(
                    f"phase0 resolved row {record['row_idx']}: "
                    f"{decision.get('classification')} (basis={decision.get('basis')})"
                )

    stats0 = prep.prepare_rows(
        packets, lookup_index,
        emit=emit0, fetch_urls=not no_fetch,
    )
    print_phase0_stats(stats0)
    work_items = stats0["work_items"]
    rate_limits = config.load_rate_limits()
    return phase0_records, stats0, work_items, rate_limits


# ---------------------------------------------------------------------------
# Stage 6 — partition: M&A rows never reach the classifier.
# ---------------------------------------------------------------------------

def _stage6_partition(
    row_mode: bool, work_items: list[WorkItem], quarters: list[str],
) -> tuple[list[WorkItem], list[WorkItem], list[WorkItem], list[WorkItem]]:
    """Returns (ma_items, classify_items, selected, excluded)."""
    if row_mode:
        return [], work_items, [], []

    ma_items = deal_profile.select_ma_items(work_items)
    classify_items = [
        it for it in work_items if it["packet"].get("acctnum") != config.MA_ACCTNUM
    ]
    selected, excluded = deal_profile.filter_items_by_quarters(ma_items, quarters)
    unparsable = sorted(
        it["packet"]["row_idx"] for it in excluded
        if deal_profile.quarter_of(it["packet"].get("period") or "") is None
    )
    if unparsable:
        console.warn(
            f"{len(unparsable)} M&A row(s) have an unparsable period and were excluded "
            f"from the sweep: {unparsable}"
        )
    return ma_items, classify_items, selected, excluded


# ---------------------------------------------------------------------------
# Stage 7 — forecast: the mandatory pre-paid gate for both phases.
# ---------------------------------------------------------------------------

def _stage7_forecast(
    selected: list[WorkItem], classify_items: list[WorkItem], model: str, rate_limits: dict | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    console.section("Forecast")
    sweep_fc = deal_profile.sweep_forecast(selected, model, rate_limits)
    print_phase1_forecast(sweep_fc)
    forecast = classify.forecast(classify_items, model, rate_limits)
    _print_phase2_forecast(forecast, rate_limits)
    return sweep_fc, forecast


# ---------------------------------------------------------------------------
# Stage 9 — Phase 1: skip the sweep (nothing in scope) or run it. (There is
# no separately-numbered stage 8; the API-key gate between the forecast and
# this stage is a one-line check, inlined in run_pipeline like cli.py's other
# commands do.)
# ---------------------------------------------------------------------------

def _stage9_phase1_sweep(
    *,
    row_mode: bool,
    ma_scope_packets: list,
    selected: list[WorkItem],
    excluded: list[WorkItem],
    quarters: list[str],
    model: str,
    human_deals_md: str | None,
    client: Any,
    rate_limits: dict | None,
    sweep_fc: dict[str, Any],
    yes: bool,
    continue_on_partial_profile: bool = False,
) -> tuple[dict[str, Any], list[DecisionRecord], bool, bool]:
    """Returns (profile, sweep_records, interrupted, declined)."""
    console.section("Phase 1: deal sweep")
    console.data("phase", {"phase": "build_profile"})
    sweep_records: list[DecisionRecord] = []

    def emit_sweep(record: DecisionRecord) -> None:
        persistence.append_record(config.DEAL_RESULTS_JSONL, record)
        sweep_records.append(record)
        console.row(record, "sweep")

    interrupted = False
    declined = False

    if row_mode:
        profile = deal_profile.load_profile(config.DEAL_PROFILE_JSON)
        if profile is not None:
            console.info(f"reusing existing deal profile at {config.DEAL_PROFILE_JSON}")
        else:
            console.info(
                "--rows with no saved deal profile -- classifying WITHOUT deal "
                "context (run `deal-profile` first if the row needs it)"
            )
        return profile, sweep_records, interrupted, declined

    if not ma_scope_packets:
        profile = empty_deal_profile()
        console.info(
            "no M&A rows in scope -- no deal profile built this run; "
            "classification proceeds with no deal context"
        )
        return profile, sweep_records, interrupted, declined

    gate_stats: dict[str, int] | None = None

    try:
        profile, sweep_stats, _sweep_usage = deal_profile.run_sweep(
            client, selected, model=model, human_deals_md=human_deals_md,
            rate_limits=rate_limits,
            cost_cap_usd=config.SPEND_CAP_MULTIPLIER * sweep_fc["cost_high_usd"],
            emit=emit_sweep,
        )
    except (KeyboardInterrupt, scheduling.SpendCapExceeded) as exc:
        interrupted = True
        profile = empty_deal_profile()
        console.clear_status()
        stopped_by = (
            "interrupted" if isinstance(exc, KeyboardInterrupt)
            else f"SPEND RAIL tripped ({exc})"
        )
        console.warn(
            f"{stopped_by} during Phase 1 (deal sweep) -- salvaging "
            f"{len(sweep_records)} swept row(s) already durable in results.jsonl..."
        )
    if not interrupted:
        for item in excluded:
            emit_sweep(
                _deal_sweep_skipped_record(item, reasoning=_quarter_skip_reasoning(quarters))
            )
        gate_stats = {
            "rows_ok": sweep_stats["rows_ok"],
            "rows_selected": sweep_stats["rows_selected"],
            "rows_failed": sweep_stats["rows_failed"],
            "invoices_read": sweep_stats["invoices_read"],
            "entries": sweep_stats["entries"],
        }

    if not interrupted and gate_stats is not None:
        console.info(
            f"Phase 1: profile built from {gate_stats['rows_ok']}/{gate_stats['rows_selected']} "
            f"M&A row(s) ({gate_stats['rows_failed']} could not gather, "
            f"{gate_stats['invoices_read']} invoice(s) read) -> {gate_stats['entries']} entr(ies)"
        )
        needs_gate = gate_stats["rows_failed"] > 0 or gate_stats["entries"] == 0
        if needs_gate and continue_on_partial_profile:
            # UI-driven run: the operator already authorized spend at the up-front
            # money gate, and this pause has no surface to answer on the process
            # screen (it would just hang until the 10-minute confirm timeout).
            # A partial profile is still usable — failed M&A rows are labeled
            # non_recurring by rule regardless — so continue to classification.
            console.info(
                "partial deal profile (sweep failures or no entries) -- continuing "
                "to classification automatically (continue_on_partial_profile)"
            )
        elif needs_gate and not yes:
            if not console.confirm(
                "Continue to classification with this partial profile? [y/N] "
            ):
                declined = True
                console.info(
                    "declined -- Phase 2 skipped; writing Phase-3 outputs for what's "
                    "done so far."
                )

    return profile, sweep_records, interrupted, declined


# ---------------------------------------------------------------------------
# Stage 10 — Phase 2: classify (only if Phase 1 wasn't interrupted/declined).
# ---------------------------------------------------------------------------

def _stage10_phase2_classify(
    *,
    interrupted: bool,
    declined: bool,
    row_mode: bool,
    profile: dict[str, Any] | None,
    human_deals_md: str | None,
    classify_items: list[WorkItem],
    model: str,
    rate_limits: dict | None,
    forecast: dict[str, Any],
    client: Any,
    stats0: prep.Phase0Stats,
) -> tuple[list[DecisionRecord], bool]:
    """Returns (classify_results, interrupted)."""
    classify_results: list[DecisionRecord] = []
    if interrupted or declined:
        return classify_results, interrupted

    # Fail-loud heads-up, read-only: what Phase 0 already found about local
    # invoice-lookup availability, ahead of the phase that actually spends on
    # invoice content. Deliberately placed here (not right after Phase 0):
    # `--dry-run` returns before this stage is ever reached, so a dry run's
    # stdout is unaffected, and `recover` never calls run_pipeline at all.
    console.banner(invoice_lookup_banner_lines(stats0))

    console.section("Phase 2: classify")
    console.data("phase", {"phase": "classify"})
    if row_mode and profile is None:
        deal_context = None
    else:
        index_text, report = prompts.deal_profile_context_index(profile)
        write_context_txt(index_text)
        print_context_report(report)
        deal_context = index_text or None

    system_blocks = prompts.build_system_prompt(deal_context, human_deals_md)
    system_tokens = sum(len(b.get("text", "")) for b in system_blocks) // config.CHARS_PER_TOKEN
    console.info(f"context: system prompt ~{system_tokens} tok per batch (cached after batch 1)")
    if rate_limits is None:
        console.warn(
            "no measured rate limits (rules/rate_limits.json absent) -- classify.py will "
            "cap workers conservatively; run `probe-limits` first for a faster run"
        )

    classify_records: list[DecisionRecord] = []

    def emit_classify(record: DecisionRecord) -> None:
        # Routed (not hardcoded to RESULTS_JSONL) because --rows mode can
        # currently send an M&A row through the classifier; routing keeps
        # each store single-account-class even then.
        persistence.append_record(_results_path_for(record), record)
        classify_records.append(record)
        console.row(record, "classify")

    try:
        classify_results = classify.run_classification(
            client, classify_items, system_blocks, model=model,
            rate_limits=rate_limits,
            cost_cap_usd=config.SPEND_CAP_MULTIPLIER * forecast["cost_high_usd"],
            emit=emit_classify,
        )
    except (KeyboardInterrupt, scheduling.SpendCapExceeded) as exc:
        interrupted = True
        classify_results = classify_records
        console.clear_status()
        stopped_by = (
            "interrupted" if isinstance(exc, KeyboardInterrupt)
            else f"SPEND RAIL tripped ({exc})"
        )
        console.warn(
            f"{stopped_by} -- salvaging {len(classify_records)} classified row(s) "
            f"already durable in results.jsonl..."
        )

    return classify_results, interrupted


# ---------------------------------------------------------------------------
# Stage 11 — Phase 3: mandatory write order (phase0 + sweep + classify, then
# summary, then Excel LAST). Runs in every case (normal, declined,
# interrupted): the operator always owes a summary + an Excel that marks what
# never got attempted.
# ---------------------------------------------------------------------------

def _stage11_output(
    *,
    workbook: Path,
    packets: list,
    phase0_records: list[DecisionRecord],
    sweep_records: list[DecisionRecord],
    classify_results: list[DecisionRecord],
    forecast: dict[str, Any],
    profile: dict[str, Any],
    scope_stats: dict[str, Any],
    run_start: float,
    interrupted: bool,
    declined: bool,
) -> int:
    console.section("Summary")
    all_records: list[DecisionRecord] = phase0_records + sweep_records + classify_results

    unprocessed_row_idxs = sorted(
        {p["row_idx"] for p in packets} - {r["row_idx"] for r in all_records}
    )

    summary = reporting.build_summary(
        all_records,
        total_rows_in=len(packets),
        forecast=forecast,
        deal_profile=profile,
    )
    summary["scope"] = scope_stats
    write_json(config.SUMMARY_JSON, summary)

    ok = excel_out.write_workbook(
        workbook, all_records, summary, config.CLASSIFIED_XLSX, deal_profile=profile,
        unprocessed_row_idxs=unprocessed_row_idxs,
    )

    print_closing_tally(summary, actual_wall_clock_min=(time.monotonic() - run_start) / 60.0)
    console.kv([
        ("results", str(config.RESULTS_JSONL)),
        ("deal results", str(config.DEAL_RESULTS_JSONL)),
        ("summary", str(config.SUMMARY_JSON)),
        ("excel", str(config.CLASSIFIED_XLSX)),
    ])
    if not ok:
        console.warn("Excel write FAILED -- see message above; run `recover` once it's closed")
    console.data("outputs", {
        "results": str(config.RESULTS_JSONL),
        "deal_results": str(config.DEAL_RESULTS_JSONL),
        "summary": str(config.SUMMARY_JSON),
        "excel": str(config.CLASSIFIED_XLSX),
        "excel_ok": ok,
    })

    console.clear_status()
    console.data("phase", {"phase": "done"})

    if interrupted:
        console.warn(
            "run: INTERRUPTED -- partial results are durable in results.jsonl; "
            "classified.xlsx marks never-attempted rows as 'not_processed'. "
            "Rerun `run --yes` to finish: there is no resume, so every "
            "in-scope row (including ones already completed above) is "
            "re-decided from scratch."
        )
        return 130
    if declined:
        return 1

    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Public entry point — cli.py's cmd_run maps an argparse.Namespace onto this.
# ---------------------------------------------------------------------------

def run_pipeline(
    *,
    workbook: Path,
    model: str | None,
    n: int | None,
    rows: str | None,
    quarters: str | None,
    months: str | None,
    min_usd: float | None,
    dry_run: bool,
    yes: bool,
    no_fetch: bool,
    user_deal_context_override: str | None = None,
    continue_on_partial_profile: bool = False,
) -> int:
    # `model` here is the FLOOR (config.DEFAULT_MODEL, or a --model override)
    # -- invoice/vision batches always upgrade to config.INVOICE_MODEL
    # per-batch inside classify.py/deal_profile.py (config.model_for_batch);
    # --model never touches that upgrade, it only overrides the floor.
    model = model or config.DEFAULT_MODEL
    sample_mode = n is not None
    row_mode = rows is not None
    if row_mode and sample_mode:
        print("ERROR: --rows and --n are mutually exclusive.", file=sys.stderr)
        return 2
    wanted_rows: set[int] = set()
    if row_mode:
        try:
            wanted_rows = _parse_rows_arg(rows)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    ingested = _stage1_ingest_and_scope(
        workbook, months, min_usd, row_mode, sample_mode, wanted_rows, n
    )
    if ingested is None:
        return 2
    packets, scope_stats = ingested

    quarter_selection = _stage2_select_quarters(row_mode, packets, quarters)
    if quarter_selection is None:
        return 2
    run_quarters, ma_scope_packets = quarter_selection

    lookup_index = _stage4_load_lookup_index()

    phase0_records, stats0, work_items, rate_limits = _stage5_phase0(
        packets, lookup_index, dry_run, sample_mode, row_mode, no_fetch,
    )

    _ma_items, classify_items, selected, excluded = _stage6_partition(
        row_mode, work_items, run_quarters
    )

    sweep_fc, forecast = _stage7_forecast(selected, classify_items, model, rate_limits)

    if dry_run:
        console.info("--dry-run: stopping before any paid API call.")
        return 0

    if not yes:
        if not console.confirm("Proceed? [y/N] "):
            console.info("run: aborted by user.")
            return 0

    run_start = time.monotonic()

    if need_api_key():
        return 1

    client = build_client()
    # Fresh limits for THE model this run uses (~1 token); the forecast above
    # used the previously measured file, which is fine for an estimate — the
    # limiter and worker derivation below get the fresh numbers.
    rate_limits = refresh_rate_limits(client, model, rate_limits)
    # UI-only kwarg (§4.3/§6.0 item 8 of the v2 UI handoff): when set, it wins
    # outright over workspace/user_deal_context.md for this run -- the two are
    # never merged, and the override is never written to disk. Omitted (the
    # CLI's only path) -> byte-identical to the on-disk loader call below.
    human_deals_md = (
        user_deal_context_override
        if user_deal_context_override is not None
        else deal_profile.load_human_deals_md()
    )

    profile, sweep_records, interrupted, declined = _stage9_phase1_sweep(
        row_mode=row_mode,
        ma_scope_packets=ma_scope_packets,
        selected=selected,
        excluded=excluded,
        quarters=run_quarters,
        model=model,
        human_deals_md=human_deals_md,
        client=client,
        rate_limits=rate_limits,
        sweep_fc=sweep_fc,
        yes=yes,
        continue_on_partial_profile=continue_on_partial_profile,
    )

    classify_results, interrupted = _stage10_phase2_classify(
        interrupted=interrupted,
        declined=declined,
        row_mode=row_mode,
        profile=profile,
        human_deals_md=human_deals_md,
        classify_items=classify_items,
        model=model,
        rate_limits=rate_limits,
        forecast=forecast,
        client=client,
        stats0=stats0,
    )

    return _stage11_output(
        workbook=workbook,
        packets=packets,
        phase0_records=phase0_records,
        sweep_records=sweep_records,
        classify_results=classify_results,
        forecast=forecast,
        profile=profile,
        scope_stats=scope_stats,
        run_start=run_start,
        interrupted=interrupted,
        declined=declined,
    )
