# Invariants checklist

The five behaviors below are the load-bearing safety guarantees of
`gna_pipeline` — stated as behavior, not code. If any of them regresses, a
change is wrong no matter how clean it looks. This file maps each one to the
test(s) that pin it and/or the code that implements it, plus the zero-cost
checks that re-prove them after a change.

NOTE: there is no resume/checkpoint mechanism anywhere in the pipeline (it was
removed entirely — `contract.packet_key`, `persistence.load_resume_state`,
`pipeline._stage3_merge_resume`, and the UI's resume-by-reupload translator
are all gone). Every run always re-decides every in-scope row from scratch.
An earlier revision of this file pinned "crash-safe resume" as invariant #2;
that invariant no longer applies and has been removed, not renumbered around
silently — see the table below for what replaced it.

Run all of these for free (no API calls):

```powershell
python -m pytest tests/ -q                     # the unit/characterization net
python -m gna_pipeline recover                 # rebuild classified.xlsx + summary.json ($0)
python tests/snapshot_canon.py workspace/results/classified.xlsx workspace/results/summary.json out.txt
#   ^ diff out.txt against data/output/tempdata/snapshot_baseline/canon_baseline.txt — an empty
#     diff (mod CRLF/LF) proves no decision moved.
python -m gna_pipeline run --dry-run --yes      # prints the forecast + spend rail, spends $0
```

---

| # | Invariant | Guarded by (tests) | Implemented in | Also re-proven by |
|---|---|---|---|---|
| 1 | **Never drop a row.** Every input row ends the run with a record — a classification, a skip, or an error record. | `test_invalid_classification_becomes_error_record_never_a_raise`, `test_malformed_enums_coerce_to_safe_defaults` (`test_invariants.py`) | `prep.prepare_rows` wraps every row in `try/except → contract.make_error_record`; `reporting.build_summary` computes `coverage.rows_with_no_record` (must be 0) | The recover snapshot's `summary.json` carries `coverage.rows_with_no_record` — byte-identical baseline means it stayed 0 |
| 2 | **The spend rail holds.** A mandatory pre-run forecast, a `--dry-run` that spends nothing, one interactive confirm, and a runtime cap that aborts past the forecast ceiling. Overshoot is bounded to one in-flight wave. | (No unit test — verified by inspection + the dry-run baseline) | `config.SPEND_CAP_MULTIPLIER` (1.15×); `scheduling.run_batches` raises `scheduling.SpendCapExceeded` per completed batch; `pipeline._stage9_phase1_sweep` / `_stage10_phase2_classify` catch it and salvage everything durable; the single `Proceed? [y/N]` gate in `pipeline.run_pipeline` (declining, or EOF on stdin via `console.confirm`, both decline safely — never auto-yes) | `run --dry-run` stdout baseline shows the forecast + `spend rail aborts past $… (1.15x high)` line; an unchanged baseline means the gate and rail wording/derivation are intact |
| 3 | **A worker never crashes the run.** Per-row / per-batch failures degrade to `human_review` error records, never an unhandled exception that kills the process. | `test_invalid_classification_becomes_error_record_never_a_raise`, `test_malformed_enums_coerce_to_safe_defaults` (`test_invariants.py`) | `prep.prepare_rows` per-row `try/except`; `classify.run_classification` anti-conflation split-and-retry, then a per-row error record (`contract.make_error_record`, reasoning `"Processing error; routed to human review."`) | — |
| 4 | **The Excel writer never corrupts or touches the source.** It writes a fresh output workbook (values-only reconstruction — the `.xlsb` source is never opened for write); a locked output fails gracefully with recovery instructions instead of raising. | `test_write_workbook_adds_deal_profile_sheet`, `test_write_workbook_without_profile_skips_sheet` (`test_excel_out.py`) | `excel_out.write_workbook` reconstructs from `ingest.read_raw_rows`, returns `False` (never raises) on `PermissionError`/`OSError` from reading the source or saving the output, pointing at `recover` | The recover snapshot is produced by this writer; a byte-identical dump proves the reconstruction + color-coding + sheets are unchanged |
| 5 | **The invoice-mismatch downgrade is an accounting rule.** When invoice evidence contradicts the model's call, the row is forced to `human_review` regardless of the proposed label. | `test_invoice_mismatch_forces_human_review_and_records_override`, `test_mismatch_on_already_human_review_needs_no_override` (`test_invariants.py`) | `classify.run_classification`'s mismatch override (records the override on the decision) | — |

## Adjacent safety behavior (not in the §2 five, but pinned)

- **The basis guard** — a `deal_profile` basis with no recognized deal is downgraded to `none` and flagged `basis_mismatch`, never guessed: `test_deal_profile_basis_without_recognized_deal_downgrades`, `test_recognized_deal_adds_deal_profile_match_flag`.
- **`human_review` always names its open question** — a `human_review` decision with no `missing_info` gets a backfill: `test_human_review_without_missing_info_gets_backfill`.
- **Local invoice resolution never guesses** — a single normalized-filename match resolves, multiple collide to `ambiguous`, none stays `no_match`: `test_invoice_local_resolve.py` (5 cases, incl. a control proving the filename fallback stays inert when the primary path already resolves).
- **The `--quarter` trailing-comma scope derivation, and `filter_scope`'s bare-YYYYMM rule** — dodges the all-digit "latest N periods" footgun, and (separately) treats a comma-free 6-digit token with a valid month as one literal period rather than a count: `test_quarter_scope_derives_trailing_comma_months`, `test_bare_yyyymm_selects_only_that_one_period_not_the_whole_file`, `test_bare_yyyymm_absent_period_raises_value_error`, `test_short_digit_string_still_means_a_count_not_a_period`, `test_six_digit_string_with_invalid_month_falls_through_to_count_branch` (`test_quarter_scope.py`).
- **Currencies are never summed together** — `reporting.build_summary` keys amounts by `(classification, currency)`; the recover snapshot's `amounts_by_classification_currency` proves it.
- **The reclass rule never reaches human review** — a row whose DESCRPN/ADDLDESC contains `reclass` is resolved in Phase 0 as `reclass` (basis `reclass_rule`) and removed from the AI workload; it is the highest-precedence Phase-0 rule, beating the negative/CLOSEGL/M&A paths: `test_reclass_text_auto_labels_reclass_in_phase0`, `test_reclass_wins_over_negative_amount`, `test_reclass_wins_over_closegl`, `test_reclass_wins_over_ma_account_and_leaves_the_sweep`, `test_reclass_record_is_a_settled_decision_with_no_error_or_sweep_debt_flag`, `test_non_reclass_row_is_unaffected` (`test_invariants.py`); implemented in `prep.is_reclass_row` + `prep.prepare_rows` Pass A.
- **A confirm gate never auto-assumes "yes" on EOF** — non-interactive stdin declines safely instead of crashing or silently proceeding: `test_confirm_declines_on_eof_never_auto_yes` (`test_ui_seams.py`).

## Last verified

`pytest` **171 passed** on `q2reformed` (Wave 2 test reconciliation after Wave
1's resume/reuse removal + Stream B company-norms / Stream D scope-and-confirm
changes, 2026-07-24) — the full unit/characterization net. This pass retired
invariant #2 ("crash-safe resume") above, since the mechanism it pinned no
longer exists, and added coverage for company-norms prompt plumbing and
`filter_scope`'s bare-YYYYMM rule. The recover-snapshot and `run --dry-run`
stdout $0 smoke checks above were **not** re-run this pass (both require the
operator's real workbook + the gitignored `snapshot_baseline/`) — re-run them
before relying on this line for anything beyond the unit/characterization net.
