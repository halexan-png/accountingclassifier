"""cli.py — `python -m gna_pipeline <cmd>` entry point.

Wires together every gna_pipeline module into the operator-facing commands
(`ingest-check`, `quarters`, `probe-limits`, `deal-profile`, `run`, `run-q2`,
`recover`). `run-q2` is a thin two-file wrapper: it flattens the Q2 A&T + G&A
workbooks (gna_pipeline.flatten_q2) into one flat sheet, then delegates to
`run` verbatim -- no downstream behavior changes.
M&A rows (config.MA_ACCTNUM) are auto-labeled non_recurring by rule and are
the sole input to Phase 1 (`deal_profile.run_sweep`); they never reach Phase 2
(`classify.run_classification`).

This module owns no business logic of its own — argparse wiring plus a thin
dispatch function per command. The full `run` orchestration lives in
`pipeline.run_pipeline`; the smaller shared operational helpers (client
construction, forecast/scope/tally printers, rate-limit probing) live there
too, since `run` and `deal-profile` both need them.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from gna_pipeline import (
    config,
    console,
    deal_profile,
    excel_out,
    ingest,
    invoice_mining,
    persistence,
    pipeline,
    prep,
    prompts,
    reporting,
    scheduling,
)
from gna_pipeline.contract import DecisionRecord

logger = logging.getLogger("gna.cli")


# ---------------------------------------------------------------------------
# Lazy --workbook resolution. argparse's own default is None (see
# build_parser) so this only ever fires inside a command that actually needs
# the workbook, never eagerly at parse time. Every workbook-reading command
# (ingest-check, deal-profile, run, AND recover -- recover reads the source
# workbook too, for scope_stats/unprocessed_row_idxs and the Excel
# reconstruction) resolves the same way: explicit --workbook wins, else the
# single .xlsb an operator dropped in workspace/. There is no data/input/
# fallback -- on a fresh clone data/ is gitignored and won't exist, so a
# fallback there would work in this dev checkout and break for every real
# operator. Missing/ambiguous workspace/ raises config.WorkbookDiscoveryError,
# a clear operator-facing message (caught in main()), not a crash.
# ---------------------------------------------------------------------------

def _resolve_workbook(args: argparse.Namespace) -> Path:
    return args.workbook if args.workbook is not None else config.discover_workbook()


# ---------------------------------------------------------------------------
# ingest-check
# ---------------------------------------------------------------------------

def cmd_ingest_check(args: argparse.Namespace) -> int:
    args.workbook = _resolve_workbook(args)
    packets, stats = ingest.read_packets(str(args.workbook), sheet=config.SHEET_NAME)
    warnings = stats["column_warnings"]

    print(f"rows read: {stats['total_data_rows']}")
    print(f"column-resolution warnings: {len(warnings)} (target 0)")
    for w in warnings:
        print(f"  - {w}")
    print(f"skipped fully-blank rows: {stats['skipped_blank_rows']}")
    print(f"blank-amount rows: {stats['blank_amount_rows']}")
    print(f"currency tally: {stats['currency_tally']}")

    negative_count = sum(1 for p in packets if p["amount"] < 0)
    closegl_count = sum(1 for p in packets if (p.get("userid") or "").strip().upper() == "CLOSEGL")
    url_count = sum(1 for p in packets if p.get("invoice_url"))

    mined_count = 0
    for p in packets:
        if p.get("invoice_url"):
            continue
        key, _truncated = invoice_mining.mine_invoice_key(p)
        if key is not None:
            mined_count += 1

    print(f"negative-amount rows: {negative_count}")
    print(f"CLOSEGL rows: {closegl_count}")
    print(f"invoice-url rows: {url_count}")
    print(f"mined invoice-token rows (no URL, non-None key): {mined_count}")

    try:
        _packets_in_scope, scope_stats = ingest.filter_scope(packets, args.months, args.min_usd)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    periods_all = scope_stats.get("periods_all") or []
    periods_selected = scope_stats.get("periods_selected") or []
    print()
    print("scope preview (--months / --min-usd):")
    all_range = f" ({min(periods_all)}..{max(periods_all)})" if periods_all else ""
    sel_range = f" ({min(periods_selected)}..{max(periods_selected)})" if periods_selected else ""
    print(f"  periods in file: {len(periods_all)}{all_range}")
    print(f"  periods selected: {len(periods_selected)}{sel_range}")
    print(
        f"  |USD Amount| >= {scope_stats.get('min_usd', 0):g}: kept {scope_stats.get('kept')} of "
        f"{scope_stats.get('total_in')} (excluded {scope_stats.get('excluded_by_period')} by period, "
        f"{scope_stats.get('excluded_by_amount')} by amount)"
    )
    return 0


# ---------------------------------------------------------------------------
# quarters — read-only, $0, no API key: which quarters does the workbook
# hold, and has the deal profile already covered each one. The listing
# `run --guided` shows the operator interactively is this exact function.
# ---------------------------------------------------------------------------

def _print_quarters_listing(packets: list, profile: dict | None) -> list[str]:
    """Numbered quarter listing shared by `quarters` and `run --guided`'s
    interactive pick. `packets` is ALL in-scope packets (not M&A-only) so the
    quarter list reflects the whole file; the M&A count per quarter is
    computed separately via deal_profile.select_ma_packets. Returns the
    ordered list of labels so a 1-based pick maps back to one."""
    available = deal_profile.quarters_available(packets)
    if not available:
        print("  (no quarters found -- check the workbook's period column)")
        return available
    profile_quarters = set((profile or {}).get("quarters", []) or [])
    ma_packets = deal_profile.select_ma_packets(packets)
    for i, q in enumerate(available, start=1):
        total = sum(1 for p in packets if deal_profile.quarter_of(p.get("period") or "") == q)
        ma_total = sum(1 for p in ma_packets if deal_profile.quarter_of(p.get("period") or "") == q)
        covered = "yes" if q in profile_quarters else "no"
        print(f"  {i}) {q}   rows: {total:>5}   M&A rows: {ma_total:>4}   profile built: {covered}")
    return available


def cmd_quarters(args: argparse.Namespace) -> int:
    """List every quarter present in the workbook (row count, M&A row count,
    whether the saved deal profile already covers it) -- an operator picks
    one of these labels for `run --quarter <LABEL>`. Read-only: no JSONL/
    profile write, no API call."""
    args.workbook = _resolve_workbook(args)
    packets, stats = ingest.read_packets(str(args.workbook), sheet=config.SHEET_NAME)
    warnings = stats["column_warnings"]
    if warnings:
        print(f"column-resolution warnings: {len(warnings)} (target 0)")
        for w in warnings:
            print(f"  - {w}")
        print()

    profile = deal_profile.load_profile(config.DEAL_PROFILE_JSON)
    print("Quarters available:")
    _print_quarters_listing(packets, profile)
    return 0


# ---------------------------------------------------------------------------
# probe-limits
# ---------------------------------------------------------------------------

def cmd_probe_limits(args: argparse.Namespace) -> int:
    if pipeline.need_api_key():
        return 1

    client = pipeline.build_client()
    try:
        payload = pipeline.measure_rate_limits(client, config.DEFAULT_MODEL)
    except Exception as exc:  # noqa: BLE001 -- give the operator one clean line, never a traceback
        # A bad key (401), a revoked key, or no network all land here. The
        # launcher's first-run key check reads this command's exit code, so a
        # clean message + non-zero return is what turns a mistyped key into a
        # "try again" prompt instead of a wall of stack trace.
        print(
            f"ERROR: could not verify the API key against Anthropic "
            f"({type(exc).__name__}: {exc}). Check that the key is correct and "
            f"that this machine can reach the internet.",
            file=sys.stderr,
        )
        return 1
    print(f"model: {payload['model']}")
    print(f"requests_limit: {payload['requests_limit']}")
    print(f"input_tokens_limit: {payload['input_tokens_limit']}")
    print(f"output_tokens_limit: {payload['output_tokens_limit']}")
    print(f"wrote {config.RATE_LIMITS_JSON}")
    return 0


# ---------------------------------------------------------------------------
# deal-profile [--quarters Q] [--model M] [--dry-run] [--yes] [--no-fetch]
# ---------------------------------------------------------------------------

def cmd_deal_profile(args: argparse.Namespace) -> int:
    """Standalone Phase 1: sweep the workbook's M&A rows for the selected
    quarter(s) and (re)build quarter_deal_profile.json. Now spends like a
    real phase -- forecast, confirm, then batched API calls -- rather than
    the old single-shot call."""
    model = args.model or config.DEFAULT_MODEL
    rate_limits = config.load_rate_limits()
    args.workbook = _resolve_workbook(args)

    # 1. Ingest + classification scope (the M&A sweep is restricted to the
    #    same --months/--min-usd window a `run` would classify).
    console.section("Ingest")
    packets, stats = ingest.read_packets(str(args.workbook), sheet=config.SHEET_NAME)
    if stats["column_warnings"]:
        console.warn(
            f"{len(stats['column_warnings'])} column-resolution warning(s); "
            f"run ingest-check for detail"
        )
    try:
        packets, scope_stats = ingest.filter_scope(packets, args.months, args.min_usd)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    pipeline.print_scope_line("deal-profile", scope_stats)

    # 2. Lookup index. The deal-profile stage always re-sweeps every M&A row
    # in scope (no resume) — deleting data/input/dealprofile/ only resets its
    # own results.jsonl history, which is never consulted to skip work.
    try:
        lookup_index = invoice_mining.load_lookup_index(config.INVOICE_LOOKUP_CSV)
    except OSError as exc:
        logger.warning(
            "deal-profile: could not load invoice lookup index at %s (%s); proceeding with "
            "an empty index",
            config.INVOICE_LOOKUP_CSV, exc,
        )
        lookup_index = {}

    # 3. M&A rows + quarter selection.
    ma_packets = deal_profile.select_ma_packets(packets)
    if not ma_packets:
        console.warn("no M&A row(s) in the workbook; skipping the sweep")
        profile = pipeline.empty_deal_profile()
        index_text, report = prompts.deal_profile_context_index(profile)
        pipeline.write_context_txt(index_text)
        pipeline.print_context_report(report)
        return 0

    available = deal_profile.quarters_available(ma_packets)
    try:
        quarters = deal_profile.parse_quarters_arg(args.quarters, available)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    console.info(f"sweeping quarter(s) {quarters} (available: {available})")

    # 4. Phase 0 over the M&A rows only (CLOSEGL/negative M&A rows resolve
    #    here and are emitted directly -- correct and desired). Gated on
    #    --dry-run exactly like cmd_run's emit0: a dry run previews the
    #    forecast with zero disk footprint, even though phase0 itself is free.
    console.section("Phase 0: prep + invoices")
    if args.dry_run:
        def emit0(record: DecisionRecord) -> None:
            return None
    else:
        def emit0(record: DecisionRecord) -> None:
            persistence.append_record(config.DEAL_RESULTS_JSONL, record)

    stats0 = prep.prepare_rows(
        ma_packets, lookup_index,
        emit=emit0, fetch_urls=not args.no_fetch,
    )
    pipeline.print_phase0_stats(stats0)
    work_items = stats0["work_items"]

    # 5. Partition by selected quarters. Standalone does NOT emit records for
    #    excluded items -- a full `run` owns their labeling.
    ma_items = deal_profile.select_ma_items(work_items)
    selected, excluded = deal_profile.filter_items_by_quarters(ma_items, quarters)
    unparsable = sorted(
        it["packet"]["row_idx"] for it in excluded
        if deal_profile.quarter_of(it["packet"].get("period") or "") is None
    )
    if unparsable:
        console.warn(
            f"{len(unparsable)} M&A row(s) have an unparsable period and are excluded from "
            f"the sweep: {unparsable}"
        )

    # 6. Forecast (mandatory pre-paid gate).
    console.section("Forecast")
    sweep_fc = deal_profile.sweep_forecast(selected, model, rate_limits)
    pipeline.print_phase1_forecast(sweep_fc)

    if args.dry_run:
        console.info("--dry-run: stopping before any paid call.")
        return 0

    if not args.yes:
        if not console.confirm("Proceed? [y/N] "):
            console.info("deal-profile: aborted by user.")
            return 0

    run_start = time.monotonic()

    if pipeline.need_api_key():
        return 1

    # 7. Sweep.
    console.section("Phase 1: deal sweep")
    client = pipeline.build_client()
    rate_limits = pipeline.refresh_rate_limits(client, model, rate_limits)
    human_deals_md = deal_profile.load_human_deals_md()

    def emit_sweep(record: DecisionRecord) -> None:
        persistence.append_record(config.DEAL_RESULTS_JSONL, record)
        console.row(record, "sweep")

    try:
        profile, sweep_stats, usage = deal_profile.run_sweep(
            client, selected, model=model, human_deals_md=human_deals_md,
            rate_limits=rate_limits,
            cost_cap_usd=config.SPEND_CAP_MULTIPLIER * sweep_fc["cost_high_usd"],
            emit=emit_sweep,
        )
    except (KeyboardInterrupt, scheduling.SpendCapExceeded) as exc:
        console.clear_status()
        stopped_by = (
            "interrupted" if isinstance(exc, KeyboardInterrupt)
            else f"stopped by the spend rail ({exc})"
        )
        console.warn(
            f"deal-profile: {stopped_by} -- swept row(s) already durable in "
            f"{config.DEAL_RESULTS_JSONL}; profile saved incrementally at "
            f"{config.DEAL_PROFILE_JSON}"
        )
        return 130

    actual_wall_clock_min = (time.monotonic() - run_start) / 60.0

    # 8. Report (no continue-prompt here -- nothing follows this phase; the
    #    profile is already durable via run_sweep's own incremental saves).
    console.section("Summary")
    console.kv([
        (
            "Phase 1",
            f"profile built from {sweep_stats['rows_ok']}/{sweep_stats['rows_selected']} "
            f"M&A row(s) ({sweep_stats['rows_failed']} could not gather, "
            f"{sweep_stats['invoices_read']} invoice(s) read) -> {sweep_stats['entries']} "
            f"entr(ies)",
        ),
        ("output", str(config.DEAL_PROFILE_JSON)),
        ("actual wall clock", f"{actual_wall_clock_min:.1f} min"),
    ])

    index_text, report = prompts.deal_profile_context_index(profile)
    pipeline.write_context_txt(index_text)
    pipeline.print_context_report(report)

    console.info(
        f"usage: input_tokens={usage.get('input_tokens', 0)} "
        f"output_tokens={usage.get('output_tokens', 0)} "
        f"cache_read={usage.get('cache_read_input_tokens', 0)} "
        f"cache_creation={usage.get('cache_creation_input_tokens', 0)}"
    )
    console.clear_status()
    return 0


# ---------------------------------------------------------------------------
# run — the full pipeline
# ---------------------------------------------------------------------------

def _quarter_scope(label: str) -> tuple[str, str]:
    """LABEL ("2026Q1") -> (quarters_arg, months_arg) exactly matching what
    `--quarter` and `--guided` both derive: quarters_arg selects just this
    one quarter for the M&A sweep; months_arg is the label's 3 YYYYMM tokens
    joined with a MANDATORY trailing comma. The trailing comma is not
    decoration -- ingest.filter_scope (see its "," in months check) takes
    the explicit-period-list branch only when a comma is present; without
    it, an all-digit 6-token month like "202601" is read as "latest 202601
    PERIODS" (i.e. the whole file), a silent footgun this dodges. Raises
    ValueError (from deal_profile.months_of_quarter) on a malformed label."""
    months = deal_profile.months_of_quarter(label)
    return label, ",".join(months) + ","


def _guided_pick_quarter(workbook: Path) -> str | None:
    """List the workbook's quarters (the same view `quarters` prints) and
    prompt the operator to pick ONE by number. Returns the picked label, or
    None if there is nothing to pick (caller reports and exits 2)."""
    packets, stats = ingest.read_packets(str(workbook), sheet=config.SHEET_NAME)
    if stats["column_warnings"]:
        console.warn(
            f"{len(stats['column_warnings'])} column-resolution warning(s); "
            f"run ingest-check for detail"
        )
    profile = deal_profile.load_profile(config.DEAL_PROFILE_JSON)
    console.section("Guided run: pick a quarter")
    available = _print_quarters_listing(packets, profile)
    if not available:
        return None

    console.clear_status()
    while True:
        try:
            raw = input(f"Pick a quarter [1-{len(available)}]: ").strip()
        except EOFError:
            print(
                "ERROR: no --yes given and stdin is not interactive; pass "
                "--quarter/--quarters instead of --guided for a headless run.",
                file=sys.stderr,
            )
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(available):
            label = available[int(raw) - 1]
            console.info(f"guided: selected quarter {label}")
            return label
        print(f"  enter a number between 1 and {len(available)}")


def cmd_run(args: argparse.Namespace) -> int:
    """Thin argparse.Namespace -> pipeline.run_pipeline(...) mapping. The
    full run orchestration (ingest through Excel output) lives there so it's
    callable -- and testable -- without an argparse Namespace.

    `--quarter`/`--guided` are resolved HERE, before pipeline.run_pipeline
    ever sees them: both just derive an ordinary (quarters=, months=) pair
    (see _quarter_scope) and mutate `args` in place, so run_pipeline's
    signature and every other caller (tests included) is untouched.
    getattr(..., None/False) defaults so a hand-built argparse.Namespace
    that predates these flags (tests/test_cli_sampling.py) keeps working."""
    args.workbook = _resolve_workbook(args)

    quarter = getattr(args, "quarter", None)
    guided = getattr(args, "guided", False)

    if guided:
        if quarter is not None or args.quarters is not None or args.months is not None:
            print(
                "ERROR: --guided picks its own quarter; remove --quarter/--quarters/--months.",
                file=sys.stderr,
            )
            return 2
        if getattr(args, "rows", None) is not None or args.n is not None:
            print("ERROR: --guided is mutually exclusive with --rows/--n.", file=sys.stderr)
            return 2
        quarter = _guided_pick_quarter(args.workbook)
        if quarter is None:
            print("ERROR: no quarters found in the workbook to pick from.", file=sys.stderr)
            return 2

    if quarter is not None:
        if args.quarters is not None or args.months is not None:
            print(
                "ERROR: --quarter is mutually exclusive with --quarters/--months.",
                file=sys.stderr,
            )
            return 2
        try:
            args.quarters, args.months = _quarter_scope(quarter)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    return pipeline.run_pipeline(
        workbook=args.workbook,
        model=args.model,
        n=args.n,
        rows=getattr(args, "rows", None),
        quarters=args.quarters,
        months=args.months,
        min_usd=args.min_usd,
        dry_run=args.dry_run,
        yes=args.yes,
        no_fetch=args.no_fetch,
    )


# ---------------------------------------------------------------------------
# run-q2 — two-file entry point: flatten A&T + G&A, then the full `run` flow
# ---------------------------------------------------------------------------

def cmd_run_q2(args: argparse.Namespace) -> int:
    """Q2's simple two-file flow: flatten the A&T + G&A workbooks into ONE
    flat sheet, then hand off to the EXACT same `run` pipeline against it.

    Nothing downstream changes -- the guided quarter pick, the
    one-quarter-drives-both scoping (that single quarter feeds BOTH the
    Phase-1 deal-profile sweep and the Phase-2 classification), and the Excel
    output (all tabs, Human Review included) are cmd_run / run_pipeline
    verbatim. This command only adds the flatten step and points `run` at its
    output, so the 128-test-green pipeline keeps proving itself.

    Defaults to --guided (interactive quarter pick, so the operator always
    sees and chooses the quarter) UNLESS an explicit scope/sample flag
    (--quarter / --quarters / --months / --n / --rows) is given.
    """
    from gna_pipeline import flatten_q2

    ga, at = Path(args.ga), Path(args.at)
    for label, path in (("--ga", ga), ("--at", at)):
        if not path.is_file():
            print(f"ERROR: {label} workbook not found: {path}", file=sys.stderr)
            return 2

    out_path = Path(args.output) if args.output else config.Q2_FLAT_XLSX
    out_path.parent.mkdir(parents=True, exist_ok=True)

    console.section("Flatten: A&T + G&A -> one flat sheet")
    headers, rows, flat_stats = flatten_q2.flatten(str(ga), str(at))
    flatten_q2.write_flat_workbook(headers, rows, str(out_path))
    for warning in flat_stats["header_detect_warnings"]:
        console.warn(warning)
    console.kv([
        ("G&A tabs included", str(len(flat_stats["tabs_included"]))),
        ("G&A tabs skipped", str(len(flat_stats["tabs_skipped"]))),
        ("A&T (M&A) rows", str(flat_stats["at_rows"])),
        ("total rows written", str(flat_stats["total_rows"])),
        ("flat workbook", str(out_path)),
    ])

    # Hand off to the proven `run` flow against the flattened workbook. Default
    # to the guided quarter pick unless the caller pinned a scope/sample.
    args.workbook = out_path
    scope_or_sample_set = (
        getattr(args, "quarter", None) is not None
        or args.quarters is not None
        or args.months is not None
        or args.n is not None
        or getattr(args, "rows", None) is not None
    )
    if not getattr(args, "guided", False) and not scope_or_sample_set:
        args.guided = True
    return cmd_run(args)


# ---------------------------------------------------------------------------
# recover — rebuild the Excel from JSONL at $0
# ---------------------------------------------------------------------------

def cmd_recover(args: argparse.Namespace) -> int:
    workbook = _resolve_workbook(args)
    raw_records = persistence.load_all_records(config.RESULTS_JSONL) + \
        persistence.load_all_records(config.DEAL_RESULTS_JSONL)

    # Fold to the LATEST record per row (file order == append order — the
    # same last-write-wins rule every re-run's re-decision relies on). A row
    # re-decided on a later run, or retried after an error record, has multiple
    # JSONL lines; counting every line double-counted the summary tallies
    # AND the USD amounts. A row lives in exactly one store (acctnum
    # routing), so concatenating the two stores never interleaves one row's
    # history.
    by_row: dict[int, DecisionRecord] = {}
    for rec in raw_records:
        by_row[rec["row_idx"]] = rec
    records = [by_row[idx] for idx in sorted(by_row)]
    superseded = len(raw_records) - len(records)
    if superseded:
        console.info(
            f"recover: folded {superseded} superseded record line(s) -- "
            f"latest per row wins"
        )

    _packets, stats = ingest.read_packets(str(workbook), sheet=config.SHEET_NAME)
    try:
        _packets_in_scope, scope_stats = ingest.filter_scope(_packets, args.months, args.min_usd)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    # Same loader as every other command: missing -> None, corrupt -> a
    # loud CorruptProfileError (caught in main), never crash-on-raw-read or
    # silently pretend "first run".
    profile = deal_profile.load_profile(config.DEAL_PROFILE_JSON)
    summary = reporting.build_summary(
        records,
        total_rows_in=len(_packets_in_scope),
        forecast=None,
        deal_profile=profile,
    )
    summary["scope"] = scope_stats
    pipeline.write_json(config.SUMMARY_JSON, summary)

    # In-scope rows with no record get the same gray "not_processed" mark an
    # interrupted `run` writes — recover used to silently leave them blank.
    unprocessed_row_idxs = sorted(
        {p["row_idx"] for p in _packets_in_scope} - set(by_row)
    )

    ok = excel_out.write_workbook(
        workbook, records, summary, config.CLASSIFIED_XLSX, deal_profile=profile,
        unprocessed_row_idxs=unprocessed_row_idxs,
    )
    pipeline.print_closing_tally(summary)
    print(
        f"\nrecover: {'wrote' if ok else 'FAILED to write'} {config.CLASSIFIED_XLSX} "
        f"(zero API cost)"
    )
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

_QUARTERS_HELP = (
    'which quarters of M&A rows feed the deal-profile sweep: a count like "2" '
    '(latest N in the file) or labels like "2026Q1" / "2025Q4,2026Q1"; '
    "default: latest quarter present"
)

_MONTHS_HELP = (
    'classification scope: latest N distinct periods in the file (default 3), '
    '"all", or explicit periods like "202601,202602"'
)
_MIN_USD_HELP = (
    "exclude rows with |USD Amount| below this from classification scope "
    "(default 999; 0 disables)"
)


def _add_shared_run_flags(p: argparse.ArgumentParser) -> None:
    """Register the scope/paid flags shared by `run` and `run-q2`. Both
    delegate to cmd_run, which reads every one of these off the Namespace, so
    they are registered in ONE place -- the two commands can never drift, and
    `run`'s flags/help stay byte-identical to before this helper existed."""
    p.add_argument(
        "--n", type=int, default=None,
        help="cheap sample run: N//2 M&A rows feed the deal-profile sweep and the remaining "
             "N - N//2 non-M&A rows flow to classification; per-row progress is printed "
             "with timestamps",
    )
    p.add_argument(
        "--rows", default=None,
        help="classify ONLY these Excel row numbers (comma-separated, e.g. 63 "
             "or 63,64). Re-decides the row even if a prior run already decided "
             "it, and reuses the saved deal profile instead of rebuilding it. "
             "Mutually exclusive with --n. Bypasses --months/--min-usd.",
    )
    p.add_argument("--quarters", default=None, help=_QUARTERS_HELP)
    p.add_argument("--months", default=None, help=_MONTHS_HELP)
    p.add_argument(
        "--quarter", default=None,
        help='pick ONE quarter label (e.g. "2026Q1") for both the M&A sweep scope and '
             "the classification months window in one shot (derives --quarters and "
             "--months from it -- see deal_profile.months_of_quarter); mutually "
             "exclusive with --quarters/--months",
    )
    p.add_argument(
        "--guided", action="store_true",
        help="interactively list the workbook's quarters, prompt for one, then run "
             "exactly like --quarter <picked> with a freshly-built deal profile",
    )
    p.add_argument("--min-usd", type=float, default=None, help=_MIN_USD_HELP)
    p.add_argument("--dry-run", action="store_true", help="print the forecast and stop before any paid call")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt(s)")
    p.add_argument("--no-fetch", action="store_true", help="skip URL invoice fetches in Phase 0")
    p.add_argument("--model", default=None, help=f"override model (default {config.DEFAULT_MODEL})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m gna_pipeline")
    parser.add_argument(
        "--workbook", type=Path, default=None,
        help="path to the source workbook (default: the single .xlsb "
             "dropped in workspace/, discovered lazily per command -- see "
             "_resolve_workbook)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest_check = sub.add_parser(
        "ingest-check",
        help=f"read {config.SHEET_NAME}, print row/warning/currency/invoice/scope stats",
    )
    p_ingest_check.add_argument("--months", default=None, help=_MONTHS_HELP)
    p_ingest_check.add_argument("--min-usd", type=float, default=None, help=_MIN_USD_HELP)

    sub.add_parser(
        "quarters",
        help="list quarters present in the workbook (row/M&A counts, profile coverage) -- $0, read-only",
    )

    sub.add_parser("probe-limits", help="measure API rate limits and write rules/rate_limits.json")

    p_deal = sub.add_parser(
        "deal-profile",
        help="run Phase 1 (the M&A deal-profile sweep) standalone, for the selected quarter(s)",
    )
    p_deal.add_argument("--quarters", default=None, help=_QUARTERS_HELP)
    p_deal.add_argument("--months", default=None, help=_MONTHS_HELP)
    p_deal.add_argument("--min-usd", type=float, default=None, help=_MIN_USD_HELP)
    p_deal.add_argument("--dry-run", action="store_true", help="print the forecast and stop before any paid call")
    p_deal.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_deal.add_argument("--no-fetch", action="store_true", help="skip URL invoice fetches in Phase 0")
    p_deal.add_argument("--model", default=None, help=f"override model (default {config.DEFAULT_MODEL})")

    p_run = sub.add_parser("run", help="run the full pipeline (Phase 1 deal sweep + Phase 2 classify)")
    _add_shared_run_flags(p_run)

    # run-q2: the two-file convenience wrapper. It flattens A&T + G&A into one
    # sheet, then delegates to cmd_run -- so it takes the SAME scope/paid flags
    # (registered via the shared helper, which is why the two commands can
    # never drift) PLUS the two input paths and an optional flat-output path.
    p_runq2 = sub.add_parser(
        "run-q2",
        help="two-file Q2 flow: flatten the A&T + G&A workbooks, then run "
             "(guided quarter pick by default)",
    )
    p_runq2.add_argument("--ga", required=True, help="path to the multi-tab G&A workbook (.xlsb or .xlsx)")
    p_runq2.add_argument("--at", required=True, help="path to the flat A&T workbook (.xlsb or .xlsx)")
    p_runq2.add_argument(
        "-o", "--output", default=None,
        help=f"where to write the flattened workbook (default {config.Q2_FLAT_XLSX})",
    )
    _add_shared_run_flags(p_runq2)

    p_recover = sub.add_parser("recover", help="rebuild classified.xlsx from results.jsonl at zero API cost")
    p_recover.add_argument("--months", default=None, help=_MONTHS_HELP)
    p_recover.add_argument("--min-usd", type=float, default=None, help=_MIN_USD_HELP)

    return parser


_DISPATCH = {
    "ingest-check": cmd_ingest_check,
    "quarters": cmd_quarters,
    "probe-limits": cmd_probe_limits,
    "deal-profile": cmd_deal_profile,
    "run": cmd_run,
    "run-q2": cmd_run_q2,
    "recover": cmd_recover,
}


def _configure_logging() -> None:
    """Surface the pipeline's logger.info/.warning progress lines through
    console.py's single writer instead of logging.basicConfig's own stream
    handler, so `logging` output shares one voice with every other line the
    CLI prints, and clears/redraws the live status line instead of tearing
    it. Noisy third-party loggers stay at WARNING so HTTP chatter doesn't
    drown the narration."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [console.ConsoleLogHandler()]
    for noisy in ("httpx", "httpcore", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    handler = _DISPATCH.get(args.command)
    if handler is None:
        parser.error(f"unknown command {args.command!r}")
        return 2
    try:
        return handler(args)
    except (
        deal_profile.CorruptProfileError,
        deal_profile.DealContextTooLargeError,
        config.WorkbookDiscoveryError,
    ) as exc:
        console.clear_status()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
