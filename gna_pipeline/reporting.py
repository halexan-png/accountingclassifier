"""reporting.py — fold DecisionRecords into the run's honest scorecard.

Pure: no IO, no printing, no mutation of inputs. Ports the fold shape of
phase2/reporting.py, widened to the v3 DecisionRecord contract (tally by phase in
addition to classification; invoice / deal_profile / flag sections; usage
priced PER RECORD via config.pricing_for(record's own model_version) and
summed, since a run can mix config.DEFAULT_MODEL (the floor) and
config.INVOICE_MODEL — see config.model_for_batch — and one blended rate
would misprice both. Amounts of different currencies are NEVER summed
together.
"""

from __future__ import annotations

import datetime
from collections import Counter
from typing import Any, get_args

from gna_pipeline import config
from gna_pipeline.contract import DecisionRecord, Flag

RunSummary = dict[str, Any]

# Zero-filled so the Run Summary sheet always shows every bucket, even at 0.
_ALL_FLAGS: tuple[str, ...] = get_args(Flag)
_YES_NO_KEYS = ("yes", "no")
_SOURCE_KEYS = ("url", "local_file")
_READ_PATH_KEYS = ("text", "vision")


def _zero_fill(counter: Counter, keys: tuple[str, ...]) -> dict[str, int]:
    out = {k: 0 for k in keys}
    out.update(counter)
    return out


def _deal_profile_entry_count(deal_profile: dict | None) -> int:
    """Best-effort entry count from the Phase-1 deal-profile dict.

    The deal-profile module is owned elsewhere and its exact on-disk shape isn't
    fixed; accept either a top-level "entries" list (most likely, mirrors
    the build_deal_profile tool's rows[]) or fall back to counting top-level keys.
    """
    if not deal_profile:
        return 0
    entries = deal_profile.get("entries")
    if isinstance(entries, list):
        return len(entries)
    return len(deal_profile)


def _forecast_estimate_usd(forecast: dict) -> float | None:
    """Midpoint of the forecast's cost range (the keys classify.forecast() uses),
    or None if the forecast doesn't carry them."""
    lo, hi = forecast.get("cost_low_usd"), forecast.get("cost_high_usd")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        return (lo + hi) / 2.0
    return None


def build_summary(
    records: list[DecisionRecord],
    *,
    total_rows_in: int,
    forecast: dict | None = None,
    deal_profile: dict | None = None,
) -> RunSummary:
    """Fold `records` into the Run Summary dict. Pure, no IO.

    `total_rows_in` is the workbook's total data-row count (all rows read by
    ingest, including CLOSEGL/negative/AI-classified) — coverage.rows_with_no_record
    must be 0 (the never-drop-a-row invariant).
    """
    class_tally: Counter[str] = Counter()
    # Same as class_tally but skips phase=="deal_profile" records (the A&T/M&A
    # account rows auto-classified non_recurring while building the deal
    # profile, config.MA_ACCTNUM). Those rows were never sent to Phase-2
    # classification and are already excluded from the Human Review Report
    # tab (exceloutputsheet.py) for the same reason — the UI Output screen's
    # tally uses this one so its "non_recurring" count matches what the
    # operator actually gets asked to review, not the auto-sweep total.
    class_tally_excl_deal_profile: Counter[str] = Counter()
    phase_tally: Counter[str] = Counter()
    amounts: dict[str, dict[str, float]] = {}
    flag_counter: Counter[str] = Counter()
    had_invoice_tally: Counter[str] = Counter()
    invoice_accessed_tally: Counter[str] = Counter()
    by_source_tally: Counter[str] = Counter()
    by_read_path_tally: Counter[str] = Counter()
    model_tally: Counter[str] = Counter()
    error_count = 0
    row_idxs: set[int] = set()
    ma_rows_total = 0
    deal_profile_records = 0
    swept_ok = 0
    invoices_read_deal_profile = 0
    usage_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    cost_actual_usd_total = 0.0

    for r in records:
        row_idxs.add(r["row_idx"])

        classification = r["decision"].get("classification", "unknown")
        class_tally[classification] += 1
        if r["phase"] != "deal_profile":
            class_tally_excl_deal_profile[classification] += 1
        phase_tally[r["phase"]] += 1

        if r["packet"].get("acctnum") == config.MA_ACCTNUM:
            ma_rows_total += 1

        if r["phase"] == "deal_profile":
            deal_profile_records += 1
            if "deal_sweep_failed" not in r["flags"] and "deal_sweep_skipped" not in r["flags"]:
                swept_ok += 1
            if r["invoice_accessed"] == "yes":
                invoices_read_deal_profile += 1

        currency = r["packet"]["currency"]
        amounts.setdefault(classification, {})
        amounts[classification][currency] = round(
            amounts[classification].get(currency, 0.0) + r["packet"]["amount"], 2
        )

        for flag in r["flags"]:
            flag_counter[flag] += 1

        had_invoice_tally[r["had_invoice"]] += 1
        invoice_accessed_tally[r["invoice_accessed"]] += 1

        invoice = r.get("invoice")
        # kind "error" summaries carry a failure reason, not a read — they
        # must not count toward the by_source/by_read_path read tallies.
        if invoice and invoice.get("kind") in ("pdf", "text"):
            if invoice.get("source"):
                by_source_tally[invoice["source"]] += 1
            if invoice.get("read_path"):
                by_read_path_tally[invoice["read_path"]] += 1

        if r.get("error"):
            error_count += 1

        usage = r.get("usage") or {}
        for key in usage_totals:
            usage_totals[key] += usage.get(key, 0)

        # Priced per-record off THAT record's own model_version, not one
        # blended rate for the whole run: a run can mix the floor model and
        # config.INVOICE_MODEL (see config.model_for_batch), and the two
        # prices differ. A record with no model_version (M&A auto-rule rows)
        # or no usage (every non-first record of a batch — see contract's
        # zero_usage()) contributes 0, same as it always has.
        if r.get("model_version"):
            model_tally[r["model_version"]] += 1
            pricing = config.pricing_for(r["model_version"])
            cost_actual_usd_total += (
                usage.get("input_tokens", 0) / 1_000_000 * pricing["input"]
                + usage.get("output_tokens", 0) / 1_000_000 * pricing["output"]
            )

    total_records = len(records)

    # Dominant model kept for back-compat (usage.model_version); models_seen
    # is the honest list when a run mixed the floor and config.INVOICE_MODEL.
    dominant_model = model_tally.most_common(1)[0][0] if model_tally else config.DEFAULT_MODEL
    models_seen = sorted(model_tally) if model_tally else [config.DEFAULT_MODEL]
    cost_actual_usd = round(cost_actual_usd_total, 4)

    summary: RunSummary = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "tally": {
            "by_classification": dict(class_tally),
            "by_classification_excl_deal_profile": dict(class_tally_excl_deal_profile),
            "by_phase": dict(phase_tally),
        },
        "amounts_by_classification_currency": amounts,
        "coverage": {
            "total_rows_in": total_rows_in,
            "records": total_records,
            "rows_with_no_record": total_rows_in - len(row_idxs),
        },
        "reclass_fired": flag_counter.get("reclass_rule", 0),
        "closegl_fired": flag_counter.get("closegl_user", 0),
        "negatives_skipped": flag_counter.get("skipped_negative", 0),
        "invoice": {
            "had_invoice": _zero_fill(had_invoice_tally, _YES_NO_KEYS),
            "invoice_accessed": _zero_fill(invoice_accessed_tally, _YES_NO_KEYS),
            "invoice_unavailable": flag_counter.get("invoice_unavailable", 0),
            "by_source": _zero_fill(by_source_tally, _SOURCE_KEYS),
            "by_read_path": _zero_fill(by_read_path_tally, _READ_PATH_KEYS),
        },
        "deal_profile": {
            "entry_count": _deal_profile_entry_count(deal_profile),
            "deal_profile_match_count": flag_counter.get("deal_profile_match", 0),
            "ma_rows_total": ma_rows_total,
            "auto_non_recurring": deal_profile_records,
            "swept_ok": swept_ok,
            "sweep_failed": flag_counter.get("deal_sweep_failed", 0),
            "sweep_skipped": flag_counter.get("deal_sweep_skipped", 0),
            "invoices_read": invoices_read_deal_profile,
        },
        "flag_counts": _zero_fill(flag_counter, _ALL_FLAGS),
        "error_count": error_count,
        "usage": {
            **usage_totals,
            "model_version": dominant_model,
            "model_versions": models_seen,
            "cost_actual_usd": cost_actual_usd,
        },
    }

    if forecast is not None:
        summary["forecast"] = dict(forecast)
        estimate = _forecast_estimate_usd(forecast)
        if estimate is not None:
            summary["forecast"]["actual_cost_usd"] = cost_actual_usd
            summary["forecast"]["delta_usd"] = round(cost_actual_usd - estimate, 4)
    else:
        summary["forecast"] = None

    return summary
