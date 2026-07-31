# RUNBOOK — G&A Non-Recurring Classifier (gna_pipeline)

Operational reference: how to set the tool up, how to run a quarter, what the
output means, and what to do when something goes wrong.

There are two ways to run this pipeline:

- **The web UI** — double-click `Start.cmd`. This is the everyday path for a
  finance user: upload two workbooks in the browser, pick a quarter, confirm
  the cost, download one Excel file. `QUICKSTART.md` is the short version of
  that flow; `HOW_IT_WORKS.md` explains the classification mechanics in plain
  language. Neither of those files covers CLI commands, flags, or
  troubleshooting internals — that's what the rest of this document is for.
- **The CLI** — `System\run_gna.ps1` (interactive menu or direct flags) or
  `python -m gna_pipeline` directly. Same pipeline, run from a terminal.
  Useful for scripted/headless runs, commands the UI doesn't expose
  (`ingest-check`, `quarters`, standalone `deal-profile`, `recover`), and
  diagnosing a run that didn't do what you expected.

Both surfaces run the exact same pipeline code and produce the same
`classified.xlsx` (§5). They differ in one important way, covered in §6: the
web UI's data lives in a **throwaway temp folder that's deleted the moment
the server stops**, so nothing persists between browser sessions. The CLI's
data lives in this repo's own `workspace\`/`data\` folders and persists
across runs until you delete it.

---

## 1. One-time setup

1. **Python 3.12 or later.** You don't need to find or install it yourself —
   both launchers (`launch_ui.ps1` for the web UI, `run_gna.ps1` for the CLI)
   look for it automatically the first time you run them (the `py` launcher,
   then `python`/`python3` on PATH, then common install directories) and
   tell you exactly what to do if they can't find one.

2. **Dependencies install themselves.** The first time you launch either
   surface, its launcher checks whether the required packages are importable
   and installs what's missing for you — `launch_ui.ps1` runs
   `pip install -e .[ui]` (pulls in `gna_pipeline` plus the web server:
   `fastapi`, `uvicorn`, `python-dotenv`, `python-multipart`); `run_gna.ps1`
   runs `pip install -r requirements.txt` (the CLI-only dependency set:
   `anthropic`, `openpyxl`, `pyxlsb`, `pypdf`, `msal`). Nothing to do here
   manually. (Manual fallback, if you ever need it:
   `<python> -m pip install -r System\requirements.txt`, or
   `<python> -m pip install -e .[ui]` from the repo root for the UI extras.)

3. **Add your API key.** Both surfaces read the same file:
   `System\.env` (copy `System\.env.example` to `System\.env` if it doesn't
   exist yet) with:
   ```
   ANTHROPIC_API_KEY=sk-ant-...your-key...
   ```
   In practice you rarely do this by hand — the first time you double-click
   `Start.cmd` with no key set, it prompts you for it in the terminal
   (verifies it with a one-token check, then writes `System\.env` itself and
   never asks again). `.env` is gitignored — never share it or commit it.
   Only paid commands (`probe-limits`, `deal-profile`, `run`/`run-q2`) need
   it; everything else works without it.

   Optional in the same file: `GRAPH_TENANT_ID` / `GRAPH_CLIENT_ID` if your
   organization's invoice links point to OneDrive/SharePoint (Microsoft
   Graph, delegated/public-client auth — no secret to leak). Leave unset and
   those links just come back `invoice_unavailable`, same as any other
   unreachable URL — no command requires this.

4. **Launch.** For the everyday web UI, double-click `Start.cmd` (see
   `QUICKSTART.md`). For the CLI, always launch through `run_gna.ps1` — it
   loads `System\.env`, finds Python, and installs dependencies as described
   above:
   ```
   .\System\run_gna.ps1                    # interactive menu (see §3)
   .\System\run_gna.ps1 <command> [flags]  # direct command
   ```

5. **The source workbook(s).** There are two accepted input shapes:

   - **Two workbooks (the standard shape)** — accounting's multi-tab **G&A**
     export (one MRI account per tab) plus the flat **A&T**
     (Acquisition & Transaction) export. The web UI's upload screen takes
     these directly and flattens them into one sheet automatically. From the
     CLI, the equivalent is the `run-q2` command:
     ```
     .\System\run_gna.ps1 run-q2 --ga "C:\path\GA_export.xlsx" --at "C:\path\AT_export.xlsx"
     ```
     This writes the flattened sheet to `workspace\q2_flat.xlsx` (override
     with `-o <path>`) and then runs exactly like `run` against it — same
     flags, same forecast/confirm, same output.
   - **One already-flat workbook** — a single `.xlsb`/`.xlsx` file already
     shaped like the flattened sheet above (sheet name
     `G&A MRI Records - With Link`). Drop exactly one such file into
     `workspace\` and every workbook-reading command (`ingest-check`,
     `quarters`, `deal-profile`, `run`, `recover`) finds it automatically; if
     `workspace\` has zero or more than one candidate, the tool tells you and
     stops before spending anything. Point at a file elsewhere with
     `--workbook <path>` **before** the command name:
     ```
     .\System\run_gna.ps1 --workbook "C:\some\other\extract.xlsx" run --dry-run
     ```

   Either way, a bad export shape (renamed tab, shifted column, moved header
   row) fails fast at the free ingest/flatten step, before anything paid
   happens.

---

## 2. Advanced — all the ways to run it (quick reference)

Every command below is available from the CLI menu's **Advanced...**
submenu or directly as `.\System\run_gna.ps1 <command> [flags]`. (The web UI
doesn't expose these individually — it drives the same `run`/`run-q2`
machinery behind its own upload/configure/run screens.)

| Command | What it does | Cost |
|---|---|---|
| `ingest-check` | Reads the workbook; prints row/warning/currency/invoice/scope stats | $0 |
| `quarters` | Lists every quarter in the workbook — row count, M&A row count, whether a deal profile already covers it | $0 |
| `probe-limits` | One 1-token API ping to measure your rate limits, saved to `rules/rate_limits.json` | ~$0 |
| `deal-profile` | Standalone Phase 1: sweeps the selected quarter's M&A rows and (re)builds the deal profile. Prints its own cost forecast and asks to proceed before spending | real cost — see forecast |
| `run` | The full pipeline against an already-flat workbook: Phase 1 deal-profile sweep + Phase 2 classify. Prints one cost forecast and asks to proceed once | real cost — see forecast |
| `run-q2` | Two-file wrapper: flattens `--ga`/`--at` into one sheet ($0), then runs exactly like `run` against it | real cost — see forecast |
| `recover` | Rebuilds `classified.xlsx` + `summary.json` from `results.jsonl` — never re-calls the model | $0 |

`run` (and `run-q2`, which shares every one of these flags) has several
modes worth knowing about — see §4 for the full flag reference:

- `run --dry-run` — does all the free work (ingest, Phase 0, invoice fetches,
  forecast) and stops before any paid call. $0.
- `run --n 12` — a cheap end-to-end rehearsal of both phases on a small
  sample, with per-row progress printed live. Pennies.
- `run --rows 63` — classifies only that one Excel row (comma-separate for
  several). About one API call.
- `run --guided` or `run --quarter 2026Q1` — classifies a full quarter (see
  §3, the everyday path). `run-q2` defaults to `--guided` automatically when
  no scope/sample flag is given.

---

## 3. The everyday path

For a finance user's everyday quarter, use the web UI (`Start.cmd`) — see
`QUICKSTART.md` for the click-by-click walkthrough (upload the two
workbooks, optionally add context/external invoices, configure quarter and
minimum USD, confirm the forecast, download `classified.xlsx`).

The CLI's equivalent, via `.\System\run_gna.ps1`, opens this menu:

```
  1) Q2 run (A&T + G&A)      Pick your two Excel files, choose a quarter, classify it
  2) Run recent activity     Classify the latest 3 months of a loaded workbook (|USD| >= $999)
  3) Run a specific quarter  Pick one quarter from a loaded workbook
  4) Advanced...             Preview cost, full-workbook run, rebuild, diagnostics
  q) quit
```

1. **Pick 1) Q2 run.** You'll be prompted to paste the full path to your
   G&A file and your A&T file (surrounding quotes are fine). The tool
   flattens them into `workspace\q2_flat.xlsx` at $0, printing tab/row
   counts as it goes.
2. **Optionally edit `workspace\user_deal_context.md`** first, with anything
   about this quarter's deals the automated M&A sweep might not catch on its
   own (a disposition that never posted to the M&A account, a vendor name to
   explicitly rule in or out, a matter number to watch for). See the comment
   block at the top of that file. There's a 2,500-word hard cap; an empty or
   missing file just means no extra context this quarter. (This is the
   CLI/file equivalent of the web UI's **Additional Context** box, which has
   its own 2,750-word cap and is session-only — see §6.)
3. **Pick a quarter.** The tool lists every quarter it found in the
   flattened workbook, e.g.:
   ```
     1) 2026Q1   rows:   543   M&A rows:   12   profile built: yes
     2) 2025Q4   rows:   601   M&A rows:    9   profile built: no
   ```
   (This is the same view `.\System\run_gna.ps1 quarters` prints on its own,
   at $0.) Type the number of the quarter you want.
4. **Read the forecast, then confirm once.** The tool builds a *fresh* deal
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
5. **Read the results.** They land in `workspace\results\`:
   `classified.xlsx` (the annotated workbook — see §5) and `summary.json`.
   The launcher opens that folder for you when the run finishes.

Options **2) Run recent activity** and **3) Run a specific quarter** do the
same thing against a workbook already sitting in `workspace\` (skips the
two-file prompt/flatten step) — equivalent to `run --months 3` and
`run --guided`/`run --quarter` respectively. Sample rehearsals, single-row
tests, choosing a quarter by flag instead of by menu, reusing an existing
deal profile, or overriding the scope window all live in **4) Advanced...**
(§2 above) or by calling `run`/`run-q2` directly with flags. `--quarter
LABEL` derives both the M&A-sweep quarter and the classification months
window from one label in a single shot; it's mutually exclusive with
`--quarters`/`--months`, same as `--guided` is with
`--rows`/`--n`/`--quarter`/`--quarters`/`--months`.

---

## 4. Useful flags

**Classification scope** (`run`, `run-q2`, `deal-profile`, `recover`,
`ingest-check`): by default, scoped to the **latest 3 distinct months**
present in the workbook, and rows whose `|USD Amount|` is **below $999** are
excluded (blank amounts are always kept; negative amounts still get
`skipped_negative` regardless of the floor).

| Flag | What it does |
|---|---|
| `--ga <path>` | (`run-q2` only, required) path to the multi-tab G&A workbook (`.xlsb` or `.xlsx`). |
| `--at <path>` | (`run-q2` only, required) path to the flat A&T workbook (`.xlsb` or `.xlsx`). |
| `-o, --output <path>` | (`run-q2` only) where to write the flattened workbook (default `workspace\q2_flat.xlsx`). |
| `--months 6` | Classification window: latest N distinct months in the file (default `3`); also accepts `all` or an explicit list like `202601,202602`. Ignored by `--rows`. |
| `--min-usd 1000` | Exclude rows with `\|USD Amount\|` below this from scope (default `999`; `0` disables it). Blank amounts always kept; negatives still get `skipped_negative`. Ignored by `--rows`. |
| `--quarters 2026Q1` | Which quarter(s) of **M&A rows** feed the deal-profile sweep — a count (latest N) or comma-separated labels. This is unrelated to `--months`: it never filters non-M&A classification. Default: the latest quarter present. |
| `--quarter 2026Q1` | Pick ONE quarter label for both the M&A-sweep scope and the classification months window in one shot. `run`/`run-q2` only; mutually exclusive with `--quarters`/`--months`. |
| `--guided` | Interactively list the workbook's quarters, prompt for one, then run exactly like `--quarter <picked>` with a freshly-built deal profile. `run`/`run-q2` only; mutually exclusive with `--rows`/`--n`/`--quarter`/`--quarters`/`--months`. `run-q2` turns this on by default when no scope/sample flag is given. |
| `--n 15` | Cheap sample rehearsal: up to `N//2` M&A rows feed the deal-profile sweep only; the rest of the N budget is filled from the non-M&A pool in scope. If fewer M&A rows exist than `N//2` (including zero), the shortfall is backfilled into classification so all N rows still get classified. `run`/`run-q2` only; mutually exclusive with `--rows`. |
| `--rows 63` | Classify only these Excel row numbers (comma-separated). Bypasses `--months`/`--min-usd` entirely and classifies against the saved deal profile as-is — row mode never runs the Phase-1 sweep. `run`/`run-q2` only; mutually exclusive with `--n`. |
| `--dry-run` | Do all the free work and stop before any paid call. `run`/`run-q2`/`deal-profile`. |
| `--yes` | Skip the confirmation prompt(s). `run`/`run-q2`/`deal-profile`. |
| `--no-fetch` | Skip fetching invoice URLs in Phase 0 (faster, but the model won't see those invoices). `run`/`run-q2`/`deal-profile`. |
| `--model <name>` | Use a different Claude model than the default. `run`/`run-q2`/`deal-profile`. |
| `--workbook <path>` | Point at a different already-flat source workbook instead of auto-discovering the one in `workspace\`. Global flag — goes BEFORE the command. Not used with `run-q2` (which builds its own flat workbook from `--ga`/`--at`). |

---

## 5. What the output means

### The workbook: `workspace\results\classified.xlsx`

This is a values-only reconstruction of your source sheet (openpyxl can't
carry over a `.xlsb`'s formatting or formulas, only its values) plus **12
appended columns**, in this order:

| Column | Meaning |
|---|---|
| `classification` | `recurring`, `non_recurring`, `human_review`, `reclass`, `skipped_negative`, or `not_processed` (a row an interrupted run never got to) |
| `basis` | What the decision was grounded in: `closegl_rule` (system close entry), `reclass_rule` (description contains "reclass"), `invoice_content` (read the attached invoice), `deal_profile` (matched a known deal), `row_text_routine` (the row's own text), `ma_account_rule` (M&A account, ruled non-recurring automatically), or `none` |
| `phase` | Which stage decided the row: `phase0` (mechanical, free), `deal_profile` (the M&A sweep), or `classify` (the paid AI step) |
| `had_invoice` | `yes`/`no` — does this row reference an invoice at all (a link or a mined invoice number)? |
| `invoice_accessed` | `yes`/`no` — was a real invoice document actually opened and read? |
| `reasoning` | Plain-English explanation of the decision |
| `evidence` | The exact quotes used, tagged `[row]` (from the ledger row) or `[invoice p.N]` (from page N of the invoice) |
| `missing_info` | For a `human_review` row, the specific open question that's blocking a confident answer |
| `invoice_pointer` | A clickable link/path to the invoice that was read, if any |
| `invoice_error` | Why a referenced invoice could NOT be read — blank when the read succeeded or nothing was referenced. Includes the plain fetch/parse failures as well as two Graph-specific ones: `graph_not_configured` (`GRAPH_TENANT_ID`/`GRAPH_CLIENT_ID` not set, §1) and `graph_not_connected` (Graph is configured but not signed in — connect from the web UI's OneDrive access step, or sign in once via the CLI) |
| `flags` | Machine tags describing what happened to the row (see below) |
| `deal_sweep_status` | For M&A rows only: the Phase-1 sweep outcome (`invoice read` / `invoice not read` / `could not gather` / `not swept`); blank for every non-M&A row |

There is no `confidence` column — the model doesn't produce one.

**Row colors:** red = `non_recurring`, yellow = `human_review` (read these
first), light green = `reclass` (a bookkeeping reclassification, settled
before it ever reaches the model), gray = `skipped_negative` and
`not_processed`, unshaded = `recurring`. Rows flagged `invoice_unavailable`
also get a light-orange highlight on their `had_invoice` and `invoice_error`
cells — a visual "the deciding document is missing" cue, independent of
classification color.

**"reclass" takes precedence over everything else in Phase 0** — a row
whose description or additional description contains "reclass" (any case,
substring match — "Reclass", "RECLASS", "reclassification") is auto-labeled
`reclass` and pulled out of the AI workload before the `CLOSEGL`, negative-
amount, or M&A-account rules ever get a chance to fire, even on an M&A-
account row that would otherwise feed the deal-profile sweep.

**How invoices are actually located and read** — the URL-vs-local-file
resolution order, the local lookup folder, and how to populate it — is
documented in `externalinvoices\README.md`, not repeated here.

### Flags (comma-separated in the `flags` column)

| Flag | Meaning |
|---|---|
| `closegl_user` | System close entry — auto-cleared as recurring, never sent to the AI |
| `reclass_rule` | Description contains "reclass" — auto-labeled `reclass`, never sent to the AI |
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
2. **Human Review Report** — an auditor-facing tab: every row classified
   `human_review` or `non_recurring` (M&A account excluded), grouped by
   vendor, with the verbatim reasoning/evidence, a summary table by
   classification, and a total dollar figure.
3. **Run Summary** — counts by classification and by phase; dollar amounts by
   classification × currency (currencies are never summed together —
   coverage's `rows_with_no_record` must be 0, the never-drop-a-row
   invariant); `closegl_fired`; `reclass_fired`; `negatives_skipped`;
   invoice-reading stats; deal-profile stats; flag counts; `error_count`;
   usage (tokens, model, actual cost vs. the forecast delta).
4. **Deal Profile** — a human-readable mirror of this quarter's
   `quarter_deal_profile.json`: one row per recognized deal, with its
   supporting rows, matter/invoice numbers, aliases, and evidence.

### `workspace\results\summary.json`

The same Run Summary data, as machine-readable JSON.

---

## 6. Where things live, and how to reset a stage

**This section describes the CLI's persistent, repo-relative paths.** The web
UI works differently: unless an operator explicitly sets `GNA_DATA_ROOT`,
`gna_server` points every one of these paths at a **throwaway temp folder**
created when the server starts and **deleted the moment it stops** (idle
timeout, the in-app Close button, or closing the terminal window). Every
upload, intermediate result, and the final `classified.xlsx` lives only in
that temp folder for the life of one server session — there is no
cross-quarter deal-profile accumulation and nothing to "reset" in the UI
beyond just closing and relaunching it. The CLI (`run_gna.ps1` /
`python -m gna_pipeline`) never sets `GNA_DATA_ROOT`, so it always uses the
paths below, and they persist across runs and relaunches.

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
- **Deal context** — `workspace\user_deal_context.md` (one file, this
  quarter's notes — see §3). In the web UI, the **Additional Context** box is
  a *one-time override* for the run about to happen — it is never written to
  this file and disappears when the session ends.
- **Doctrine/policy files** — `doctrines\classifier.md` (the classification
  rulebook), `doctrines\dealbuilder.md` (Phase-1 sweep instructions), and
  `data\input\companynorm.md` (standing, company-wide notes — not
  per-quarter). These are code-adjacent program config, not business data:
  they live under the repo root even in web-UI mode, so a UI session's
  ephemeral temp workspace does **not** wipe them, and they persist across UI
  sessions the same way they persist across CLI runs. The web UI's Settings
  screen edits these three files directly (permanent writes); so does editing
  them by hand. See `HOW_IT_WORKS.md` for what each one is for and the order
  of authority between them.
- Deleting neither the deal-profile nor classification folder is the normal
  case. A rerun always re-decides every in-scope row from scratch — there is
  no skip-on-rerun; the JSONL files are audit trails, not resume state. To
  salvage a partial run's completed rows without paying again, use `recover`
  (see §7).

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

(In the web UI, closing the browser tab does not stop the run — the server
keeps going in its terminal window; reopening the tab reconnects to it.
Stopping the *server* mid-run, however, wipes the ephemeral workspace along
with everything else — see §6 — so there's nothing left to `recover` from.)

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
(or re-pick the same quarter via `--guided`/`--quarter`, or re-run `run-q2`
with the same `--ga`/`--at`). There is no resume — see §7 — so every
in-scope row is decided again, including ones that already completed.

**"Your session timed out" (web UI only)** — the server stopped itself after
about fifteen minutes with no activity and no run in progress, and cleared
the temp workspace with it (§6). This is expected, not a fault. Relaunch
`Start.cmd` and start again; set `GNA_UI_IDLE_TIMEOUT_MIN=0` in `System\.env`
to disable the timeout if you need the server to sit idle indefinitely.

**Rate limits changed (new API plan/tier)** — re-measure them:
```
.\System\run_gna.ps1 probe-limits
```

**An invoice link or invoice number won't resolve** — this is not an error.
The row is flagged `invoice_unavailable` and still gets classified using its
own row text (account, amount, description). Nothing is skipped. If
`invoice_error` reads `graph_not_configured` or `graph_not_connected`, see
the OneDrive/Graph note in §1/§5. Otherwise see `externalinvoices\README.md`
for how resolution actually works and how to add more invoices to the local
library.

**"No .xlsb workbook found in workspace\\"** or **"Found N .xlsb files...
keep exactly one"** — drop your already-flat workbook into `workspace\`, and
make sure there's exactly one candidate file there (or pass `--workbook
<path>` to point at a specific file elsewhere). If you're starting from the
two raw accounting exports instead, use `run-q2 --ga ... --at ...` (or the
web UI's upload screen) rather than dropping either of them into `workspace\`
directly.

**"ANTHROPIC_API_KEY is not set"** — check that `System\.env` exists and has
a line like `ANTHROPIC_API_KEY=sk-ant-...` with no extra quotes or spaces.

**A row failed with an API error mid-run** — its `classification` shows
`human_review` with "Processing error" in `reasoning`. Error rows are never
frozen: run again and they're retried automatically (there is no
skip-on-rerun either way, so every in-scope row — failed or not — is decided
again).

---

## 9. About the source data

The pipeline reads a flattened list of rows from sheet
`G&A MRI Records - With Link`. In the standard two-workbook shape, that sheet
is produced by flattening the operator's multi-tab **G&A** export (one MRI
account per tab) together with the flat **A&T** export; `run`/`deal-profile`/
`ingest-check`/`recover` can also read an already-flat `.xlsb`/`.xlsx` file
shaped the same way. The flattened list typically holds well over a single
quarter's worth of rows, which is why classification defaults to a rolling
window (`--months`/`--min-usd`, §4) instead of reading everything.

The Acquisition & Transaction account, `MR58200000`, is the M&A account: every
row on it is auto-labeled `non_recurring` by rule and is the sole input to
the Phase-1 deal-profile sweep — it's never sent to Phase 2. The sweep is
live; the "no M&A rows in scope" skip path only fires when a particular
scope or quarter selection happens to exclude all of them. The one exception
is described in §5: a row whose description contains "reclass" is pulled out
as `reclass` before the M&A-account rule (or `CLOSEGL`, or the negative-
amount check) ever runs, M&A account or not.

`classified.xlsx` is a values-only reconstruction of the source sheet (see
§5) — any conditional formatting, formulas, or styling from the original
workbook are not carried over, only the raw values plus the appended pipeline
columns. Row-hash format (`ROW_HASH_VERSION`) fingerprints each decision
record for the audit log; it is bumped when the row schema changes so records
written under different schemas are never conflated. It is not a resume/skip
key — every run re-decides every row regardless.
