# RUNBOOK — G&A Non-Recurring Classifier (gna_pipeline)

Operational reference: how to set the tool up, how to run a quarter, what the
output means, and what to do when something goes wrong.

If you just want to get started, read `QUICKSTART.md` instead — it's the
short finance-facing version of §1 and §3 below. `HOW_IT_WORKS.md` explains
the mechanics in plain language (what Phase 0/1/2 actually do). This file is
the complete command/flag/output reference for when you need more than the
quick-start.

---

## 1. One-time setup

1. **Python 3.12 or later.** You don't need to find or install it yourself —
   `run_gna.ps1` (the launcher, see step 4) looks for it automatically the
   first time you run it (the `py` launcher, then `python`/`python3` on PATH,
   then common install directories) and tells you exactly what to do if it
   can't find one.

2. **Dependencies install themselves.** The first time you launch the tool,
   `run_gna.ps1` checks whether `anthropic`, `openpyxl`, `pyxlsb`, and `pypdf`
   are importable and, if not, runs `pip install -r requirements.txt` for you.
   Nothing to do here manually. (Manual fallback, if you ever need it:
   `<python> -m pip install -r System\requirements.txt`.)

3. **Add your API key.** Copy `.env.example` to `.env` inside the `System`
   folder (both live there, next to `run_gna.ps1`) and set:
   ```
   ANTHROPIC_API_KEY=sk-ant-...your-key...
   ```
   `.env` is gitignored — never share it or commit it. Only the paid commands
   (`probe-limits`, `deal-profile`, `run`) need this; everything else works
   without it.

4. **Always launch through `run_gna.ps1`** — it loads `.env`, finds Python,
   and installs dependencies as described above:
   ```
   .\System\run_gna.ps1                    # interactive menu (see §3)
   .\System\run_gna.ps1 <command> [flags]  # direct command
   ```

5. **The source workbook.** Drop your G&A records `.xlsb` file straight into
   `workspace\` — keep exactly one `.xlsb` file there. Every command that
   needs the workbook finds it automatically; if `workspace\` has zero or
   more than one `.xlsb` file, the tool tells you and stops before spending
   anything. To point at a workbook somewhere else instead, pass
   `--workbook <path>` **before** the command name:
   ```
   .\System\run_gna.ps1 --workbook "C:\some\other\extract.xlsb" run --dry-run
   ```
   The sheet the pipeline reads is always named `G&A MRI Records - With
   Link`.

---

## 2. Advanced — all the ways to run it (quick reference)

Every command below is available from the launcher's interactive menu
(**2) Advanced...**) or directly as `.\System\run_gna.ps1 <command> [flags]`.

| Command | What it does | Cost |
|---|---|---|
| `ingest-check` | Reads the workbook; prints row/warning/currency/invoice/scope stats | $0 |
| `quarters` | Lists every quarter in the workbook — row count, M&A row count, whether a deal profile already covers it | $0 |
| `probe-limits` | One 1-token API ping to measure your rate limits, saved to `rules/rate_limits.json` | ~$0 |
| `deal-profile` | Standalone Phase 1: sweeps the selected quarter's M&A rows and (re)builds the deal profile. Prints its own cost forecast and asks to proceed before spending | real cost — see forecast |
| `run` | The full pipeline: Phase 1 deal-profile sweep + Phase 2 classify. Prints one cost forecast and asks to proceed once | real cost — see forecast |
| `recover` | Rebuilds `classified.xlsx` + `summary.json` from `results.jsonl` — never re-calls the model | $0 |

`run` has several modes worth knowing about (all still just `run` with
different flags — see §4 for the full flag reference):

- `run --dry-run` — does all the free work (ingest, Phase 0, invoice fetches,
  forecast) and stops before any paid call. $0.
- `run --n 12` — a cheap end-to-end rehearsal of both phases on a small
  sample, with per-row progress printed live. Pennies.
- `run --rows 63` — classifies only that one Excel row (comma-separate for
  several). About one API call.
- `run --guided` or `run --quarter 2026Q1` — classifies a full quarter (see
  §3, the everyday path).

---

## 3. The everyday path

This is what you do every quarter.

1. **Drop the workbook in `workspace\`.** Keep exactly one `.xlsb` file
   there.
2. **Optionally edit `workspace\user_deal_context.md`** with anything about this
   quarter's deals the automated M&A sweep might not catch on its own (a
   disposition that never posted to the M&A account, a vendor name to
   explicitly rule in or out, a matter number to watch for). See the comment
   block at the top of that file. There's a 2,500-word hard cap (a heads-up
   appears past ~1,250 words); an empty or missing file just means no extra
   context this quarter.
3. **Launch the tool and pick the guided run:**
   ```
   .\System\run_gna.ps1
   ```
   then choose **1) Run a quarter (guided)** — or run
   `.\System\run_gna.ps1 run --guided` directly.
4. **Pick a quarter.** The tool lists every quarter it found in the workbook,
   e.g.:
   ```
     1) 2026Q1   rows:   543   M&A rows:   12   profile built: yes
     2) 2025Q4   rows:   601   M&A rows:    9   profile built: no
   ```
   (This is the same view `.\System\run_gna.ps1 quarters` prints on its own, at
   $0.) Type the number of the quarter you want.
5. **Read the forecast, then confirm once.** The tool builds a *fresh* deal
   profile for that quarter (sweeping its M&A rows) and prints one real cost
   forecast covering both the deal-profile sweep and the classification pass
   — rows, batches, token ranges, an estimated dollar range, the spend rail,
   worker count, estimated wall clock — then asks:
   ```
   Proceed? [y/N]
   ```
   Type `y` to spend the money and classify the quarter. (The only other
   prompt you might see is a conditional safety check — *"Continue to
   classification with this partial profile? [y/N]"* — which only appears if
   the deal-profile sweep came back with failures or zero entries. Everyday
   runs never see it.)
6. **Read the results.** They land in `workspace\results\`:
   `classified.xlsx` (the annotated workbook — see §5) and `summary.json`.
   The launcher opens that folder for you when the run finishes.

Everything else — sample rehearsals, single-row tests, choosing a quarter by
flag instead of by menu, reusing an existing deal profile, overriding the
scope window — lives in **2) Advanced...** (§2 above) or `.\System\run_gna.ps1 run
--quarter 2026Q1` directly. `--quarter LABEL` derives both the M&A-sweep
quarter and the classification months window from one label in a single
shot; it's mutually exclusive with `--quarters`/`--months`, same as
`--guided` is with `--rows`/`--n`/`--quarter`/`--quarters`/`--months`.

---

## 4. Useful flags

**Classification scope** (`run`, `deal-profile`, `recover`, `ingest-check`):
by default, scoped to the **latest 3 distinct months** present in the
workbook, and rows whose `|USD Amount|` is **below $999** are excluded
(blank amounts are always kept; negative amounts still get
`skipped_negative` regardless of the floor).

| Flag | What it does |
|---|---|
| `--months 6` | Classification window: latest N distinct months in the file (default `3`); also accepts `all` or an explicit list like `202601,202602`. Ignored by `--rows`. |
| `--min-usd 1000` | Exclude rows with `\|USD Amount\|` below this from scope (default `999`; `0` disables it). Blank amounts always kept; negatives still get `skipped_negative`. Ignored by `--rows`. |
| `--quarters 2026Q1` | Which quarter(s) of **M&A rows** feed the deal-profile sweep — a count (latest N) or comma-separated labels. This is unrelated to `--months`: it never filters non-M&A classification. Default: the latest quarter present. |
| `--quarter 2026Q1` | Pick ONE quarter label for both the M&A-sweep scope and the classification months window in one shot. `run` only; mutually exclusive with `--quarters`/`--months`. |
| `--guided` | Interactively list the workbook's quarters, prompt for one, then run exactly like `--quarter <picked>` with a freshly-built deal profile. `run` only; mutually exclusive with `--rows`/`--n`/`--quarter`/`--quarters`/`--months`. |
| `--n 15` | Cheap sample rehearsal: up to `N//2` M&A rows feed the deal-profile sweep only; the rest of the N budget is filled from the non-M&A pool in scope. If fewer M&A rows exist than `N//2` (including zero), the shortfall is backfilled into classification so all N rows still get classified. `run` only; mutually exclusive with `--rows`. |
| `--rows 63` | Classify only these Excel row numbers (comma-separated). Bypasses `--months`/`--min-usd` entirely and classifies against the saved deal profile as-is — row mode never runs the Phase-1 sweep. `run` only; mutually exclusive with `--n`. |
| `--dry-run` | Do all the free work and stop before any paid call. `run`/`deal-profile`. |
| `--yes` | Skip the confirmation prompt(s). `run`/`deal-profile`. |
| `--no-fetch` | Skip fetching invoice URLs in Phase 0 (faster, but the model won't see those invoices). `run`/`deal-profile`. |
| `--model <name>` | Use a different Claude model than the default. `run`/`deal-profile`. |
| `--workbook <path>` | Point at a different source workbook instead of auto-discovering the one in `workspace\`. Global flag — goes BEFORE the command. |

---

## 5. What the output means

### The workbook: `workspace\results\classified.xlsx`

This is a values-only reconstruction of your source sheet (openpyxl can't
carry over a `.xlsb`'s formatting or formulas, only its values) plus **12
appended columns**, in this order:

| Column | Meaning |
|---|---|
| `classification` | `recurring`, `non_recurring`, `human_review`, `skipped_negative`, or `not_processed` (a row an interrupted run never got to) |
| `basis` | What the decision was grounded in: `closegl_rule` (system close entry), `invoice_content` (read the attached invoice), `deal_profile` (matched a known deal), `row_text_routine` (the row's own text), `ma_account_rule` (M&A account, ruled non-recurring automatically), or `none` |
| `phase` | Which stage decided the row: `phase0` (mechanical, free), `deal_profile` (the M&A sweep), or `classify` (the paid AI step) |
| `had_invoice` | `yes`/`no` — does this row reference an invoice at all (a link or a mined invoice number)? |
| `invoice_accessed` | `yes`/`no` — was a real invoice document actually opened and read? |
| `reasoning` | Plain-English explanation of the decision |
| `evidence` | The exact quotes used, tagged `[row]` (from the ledger row) or `[invoice p.N]` (from page N of the invoice) |
| `missing_info` | For a `human_review` row, the specific open question that's blocking a confident answer |
| `invoice_pointer` | A clickable link/path to the invoice that was read, if any |
| `invoice_error` | Why a referenced invoice could NOT be read — blank when the read succeeded or nothing was referenced |
| `flags` | Machine tags describing what happened to the row (see below) |
| `deal_sweep_status` | For M&A rows only: the Phase-1 sweep outcome (`invoice read` / `invoice not read` / `could not gather` / `not swept`); blank for every non-M&A row |

There is no `confidence` column — the model doesn't produce one.

**Row colors:** red = `non_recurring`, yellow = `human_review` (read these
first), gray = `skipped_negative` and `not_processed`, unshaded =
`recurring`. Rows flagged `invoice_unavailable` also get a light-orange
highlight on their `had_invoice` and `invoice_error` cells — a visual "the
deciding document is missing" cue, independent of classification color.

**How invoices are actually located and read** — the URL-vs-local-file
resolution order, the local lookup folder, and how to populate it — is
documented in `externalinvoices\README.md`, not repeated here.

### Flags (comma-separated in the `flags` column)

| Flag | Meaning |
|---|---|
| `closegl_user` | System close entry — auto-cleared as recurring, never sent to the AI |
| `skipped_negative` | Negative amount — shown but not classified this run |
| `had_invoice` | This row references an invoice |
| `invoice_accessed` | A real document was opened and read |
| `invoice_unavailable` | An invoice was referenced but couldn't be opened — the row was still classified on its own text |
| `deal_profile_match` | Matched a name from this quarter's deal profile — a reason for suspicion, not proof by itself |
| `amount_blank` | The dollar amount was blank in the source data |
| `deal_sweep_failed` | The Phase-1 sweep could not gather deal info for this M&A row (API/parse failure) |
| `deal_sweep_skipped` | This M&A row was not swept this run (outside the selected sweep quarter(s)) |
| `basis_mismatch` | The model claimed `basis="deal_profile"` but didn't actually recognize a deal — downgraded to `none` rather than guessed |

### Sheets in `classified.xlsx`

1. The annotated G&A sheet (opens first).
2. **Run Summary** — counts by classification and by phase; dollar amounts by
   classification × currency (currencies are never summed together —
   coverage's `rows_with_no_record` must be 0, the never-drop-a-row
   invariant); `closegl_fired`; `negatives_skipped`; invoice-reading stats;
   deal-profile stats; flag counts; `error_count`; usage (tokens, model,
   actual cost vs. the forecast delta).
3. **Deal Profile** — a human-readable mirror of this quarter's
   `quarter_deal_profile.json`: one row per recognized deal, with its
   supporting rows, matter/invoice numbers, aliases, and evidence.

### `workspace\results\summary.json`

The same Run Summary data, as machine-readable JSON.

---

## 6. Where things live, and how to reset a stage

- **Deal-profile stage** — `data\input\dealprofile\`: `results.jsonl` (the
  sweep's durable append-only log — an audit trail, not consulted to skip
  work), `quarter_deal_profile.json` (the accumulated cross-quarter deal
  vocabulary; loaded as-is by `--rows`), `deal_profile_context.txt` (the exact
  deal-index text handed to the classifier). Every run already re-sweeps this
  quarter's M&A rows from scratch; delete this folder to also clear the
  accumulated deal vocabulary.
- **Classification stage** — `data\output\results\`: `results.jsonl` (Phase-2's
  durable append-only log — an audit trail for `recover`, never consulted to
  skip a row). Every run reclassifies every in-scope row regardless.
- **Deal context** — `workspace\user_deal_context.md` (one file, this quarter's
  notes — see §3).
- Deleting neither folder is the normal case. A rerun always re-decides every
  in-scope row from scratch — there is no skip-on-rerun; the JSONL files are
  audit trails, not resume state. To salvage a partial run's completed rows
  without paying again, use `recover` (see §7).

---

## 7. Interrupted runs (there is no resume)

Every decision is written to durable JSONL the instant it's made — but that
log is an audit trail, not a resume checkpoint. If a run crashes or you close
the window mid-run, you have two choices:

- **Re-run it** — every in-scope row is decided again from scratch. There is
  no skip-on-rerun, so you will pay again for rows that already completed once.
  This is deliberate: a run never silently reuses a stale or partial earlier
  decision.
- **`recover`** — rebuild `summary.json` and `classified.xlsx` from the
  decisions already on disk at $0 (no model calls). Completed rows are kept;
  in-scope rows with no saved decision are marked gray `not_processed`.

---

## 8. When something goes wrong

**"Excel write skipped: file is locked"** — someone has `classified.xlsx`
open in Excel. Close it, then run:
```
.\System\run_gna.ps1 recover
```
This rebuilds the Excel file from the already-saved results at **zero
cost** — no rows are reclassified.

**The run crashed or you closed the window mid-run** — just run it again:
```
.\System\run_gna.ps1 run --yes
```
(or re-pick the same quarter via `--guided`/`--quarter`). Every row already
classified is remembered and skipped; only the unfinished rows are sent.

**Rate limits changed (new API plan/tier)** — re-measure them:
```
.\System\run_gna.ps1 probe-limits
```

**An invoice link or invoice number won't resolve** — this is not an error.
The row is flagged `invoice_unavailable` and still gets classified using its
own row text (account, amount, description). Nothing is skipped. See
`externalinvoices\README.md` for how resolution actually works and how to
add more invoices to the local library.

**"No .xlsb workbook found in workspace\\"** or **"Found N .xlsb files...
keep exactly one"** — drop your G&A records workbook into `workspace\`, and
make sure there's exactly one `.xlsb` file there (or pass `--workbook
<path>` to point at a specific file elsewhere).

**"ANTHROPIC_API_KEY is not set"** — check your `.env` file exists in the
`System` folder and has a line like `ANTHROPIC_API_KEY=sk-ant-...` with no
extra quotes or spaces.

**A row failed with an API error mid-run** — its `classification` shows
`human_review` with "Processing error" in `reasoning`. Error rows are never
frozen: run again and they're retried automatically (finished rows are
still skipped, so you only pay for the ones that failed).

---

## 9. About the source data

The pipeline reads a binary Excel extract (`.xlsb`, via the `pyxlsb`
package), sheet `G&A MRI Records - With Link`. It typically holds well over
a single quarter's worth of rows, which is why classification defaults to a
rolling window (`--months`/`--min-usd`, §4) instead of reading everything.

The Acquisition & Transaction account, `MR58200000`, is the M&A account: every
row on it is auto-labeled `non_recurring` by rule and is the sole input to
the Phase-1 deal-profile sweep — it's never sent to Phase 2. The sweep is
live; the "no M&A rows in scope" skip path only fires when a particular
scope or quarter selection happens to exclude all of them.

`classified.xlsx` is a values-only reconstruction of the source sheet (see
§5) — any conditional formatting, formulas, or styling from the original
`.xlsb` are not carried over, only the raw values plus the appended pipeline
columns. Row-hash format (`ROW_HASH_VERSION`) fingerprints each decision
record for the audit log; it is bumped when the row schema changes so records
written under different schemas are never conflated. It is not a resume/skip
key — every run re-decides every row regardless.
