# gna_pipeline — module map

Run as `python -m gna_pipeline <cmd>` (via `run_gna.ps1`). Pipeline order:
ingest → Phase 0 prep ($0) → forecast gate (single confirm) → Phase 1 deal
profile → Phase 2 batch classify → Phase 3 summary + Excel. `run` is
orchestrated by `pipeline.run_pipeline`; `cli.py` itself owns no business
logic — just argparse wiring and a thin per-command dispatch function.

Six commands: `ingest-check`, `quarters`, `probe-limits`, `deal-profile`,
`run`, `recover`.

| File | What it does |
|---|---|
| `__init__.py` | Package marker; docstring only, no code. |
| `__main__.py` | Entry point; forwards to `cli.main()`. |
| `cli.py` | argparse wiring + a thin dispatch function per command (`ingest-check`, `quarters`, `probe-limits`, `deal-profile`, `run`, `recover`). No business logic of its own — `run`'s full orchestration lives in `pipeline.run_pipeline`; `cmd_run` is just an argparse.Namespace -> keyword-args mapping. |
| `classify.py` | Phase 2: token-budget batching (via `scheduling.py`), pre-run cost/time forecast, concurrent batch calls with the forced `classify_rows` tool, anti-conflation check + split-and-retry, invoice-mismatch -> human_review override. |
| `config.py` | All paths (workspace discovery, stage-scoped `data/input`/`data/output` folders), model id (`claude-sonnet-5`), token/batching constants, spend-cap multiplier (1.15x forecast high), rate-limit loading, `discover_workbook`. |
| `console.py` | The single output voice for the CLI: `section`/`banner`/`info`/`warn`/`kv`/`row`/`confirm`/`status`/`Progress`/`ConsoleLogHandler` — hand-rolled, no `rich`; thread-safe, ASCII-only, routes `logging` output through the same writer. |
| `contract.py` | The only place row/record shapes live: `RowPacket`, `WorkItem`, `DecisionRecord`, the `Flag`/`Basis` enums, `row_hash` (version tag `ROW_HASH_VERSION`, currently "v4") — an audit-log fingerprint, not a resume key — record builders, and `invoice_summary_for_record` (the single shared per-record invoice-summary rule). |
| `deal_profile.py` | Phase 1: the M&A deal-profile sweep over account `MR58200000` extracting the quarter's deal vocabulary with evidence; the quarter helpers (`quarters_available`, `months_of_quarter`, `parse_quarters_arg`); loads `workspace/user_deal_context.md` (`load_human_deals_md`, 2,500-word cap); profile persistence (`load_profile`, atomic save, `CorruptProfileError`, `DealContextTooLargeError`). Doubles as the API smoke test. |
| `excel_out.py` | Writes `workspace/results/classified.xlsx` last: a values-only reconstruction of the source sheet (openpyxl can't read `.xlsb` formatting) + 12 appended columns, color-coding, plus "Run Summary" and "Deal Profile" sheets; lock-safe (returns False instead of raising; `recover` rebuilds at $0). |
| `ingest.py` | Reads the sheet `G&A MRI Records - With Link` (`config.SHEET_NAME`) into `RowPacket`s: alias-based column resolution (wrong-column + USD-amount-pinning warnings), NULL-sentinel cleanup, normalization, read stats; also `filter_scope` (the `--months`/`--min-usd` window). |
| `invoice_mining.py` | Mines invoice keys from row text (regex patterns over `descrptn`/`adddesc`, priority-ordered), normalizes them, resolves against `externalinvoices/` (per-invoice PDFs directly in it) with entityid tie-breaks and a bare-filename fallback. |
| `invoice_read.py` | Fetches/reads invoice documents at $0: URL fetch with backoff + login/HTML rejection, local PDF load, pypdf text extraction with vision sub-PDF fallback, sha256 + token estimates. |
| `persistence.py` | Durable JSONL audit log: append with flush+fsync, tolerant reload (`append_record`/`load_all_records`), read back by `recover`. No resume index — every run re-decides every row; the log is never consulted to skip work. |
| `pipeline.py` | The `run_pipeline` spine cli.py's `run` delegates to — the readable staged sequence: ingest -> scope -> quarter-select -> invoice-index -> Phase 0 -> partition -> forecast -> Phase 1 sweep -> Phase 2 classify -> Phase 3 summary+Excel — plus the small operational helpers (`build_client`, forecast/scope/tally printers, rate-limit probing) shared by `run`/`deal-profile`/`recover`. |
| `prep.py` | Phase-0 orchestrator: CLOSEGL auto-recurring (with the deal-language guard) + negative-skip triage, invoice resolution (URL priority, else mined key -> local dir); builds `WorkItem`s; never drops a row. |
| `prompts.py` | Domain context + the classifier doctrine loaded live from `doctrines/classifier.md` (`load_baseline_instructions`), optional `workspace/user_deal_context.md`, the compact deal-identifier index, tool schemas, byte-stable prompt-cached system-prompt assembly, batch user content, per-row token estimator. |
| `reporting.py` | Pure fold of all `DecisionRecord`s into `summary.json`: tallies by class/phase/flag, priced usage, forecast-vs-actual delta. Never mixes currencies. |
| `scheduling.py` | The scheduler shared by Phase 1 and Phase 2: `size_batches`, rolling-60s `RateLimiter`/`build_limiter`, `compute_max_workers`, `run_batches`, and the `SpendCapExceeded` exception. Imported by both `classify.py` and `deal_profile.py`. |
