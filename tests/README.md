# tests/

Regression tests for the `gna_pipeline` (G&A non-recurring classifier).

## What this is — and isn't

These tests are a **safety net, not part of the product.** Nothing in
`gna_pipeline` imports anything here. You can delete this whole folder and every
run — `--n`, `--rows`, full runs — behaves identically. (There is no resume/
checkpoint mechanism any more: every run always re-decides every in-scope row
from scratch; see `test_persistence.py`.)

They exist to answer one question fast and for free: **"did my last code change
break something?"** Every test here either locks down a real past bug or pins a
load-bearing accounting rule so a refactor can't silently change it.

They **do not** test AI classification quality. They use synthetic and stubbed
data only — **no API calls, no tokens, no cost.** The whole suite runs in a
couple of seconds.

## When to run it

Nothing runs these automatically — there's no CI, no git hook, no build step.
Run them yourself when it helps:

- **After editing `gna_pipeline` code**, before a real (paid) run — catch a
  regression for free instead of discovering it mid-run.
- **Before committing.**
- **When picking the work back up**, as a quick "still wired correctly?" check.

```powershell
python -m pytest tests/ -q
```

See `INVARIANTS.md` in this folder for how each §2 safety invariant maps to the
test(s) and code that guard it, plus the zero-cost checks that re-prove them.

## What each file guards

### `test_invariants.py` — the accounting-correctness rules
Characterization tests for behavior that must survive any refactor verbatim:
the **invoice-mismatch downgrade** (invoice evidence contradicting the model's
call forces `human_review` and records the override), the **basis guard**
(`deal_profile` basis with no recognized deal is downgraded and flagged),
**never drop a row** (an invalid model output becomes an error record, not a
crash), the **audit fingerprint** (`contract.row_hash`, pinned to a golden
hash value — it is NOT a resume key; there is no resume mechanism any more),
and the invoice-token extraction golden table.

### `test_persistence.py` — results.jsonl durability
The pipeline writes each row's decision to `results.jsonl` so `recover` can
rebuild the Excel + summary at $0 from history alone. These tests prove
`append_record`/`load_all_records` round-trip every record (including error
records) in file order, tolerate a malformed/truncated trailing line, and
return an empty list for a missing file. There is no resume/skip filtering
here — that mechanism (`persistence.load_resume_state`, `contract.packet_key`)
was removed entirely; every run re-decides every in-scope row.

### `test_cli_sampling.py` — the `--n` and `--rows` run flags
Cheap sample/rehearsal runs before a full run:
- `--n N` splits the sample — half feed the deal-profile sweep (never
  classified), half get classified — and still finds M&A rows even when they
  sit at the end of the file. Covers the `N//2` split, the M&A-shortfall case,
  and the full-run partition (M&A rows sweep, everything else classifies).
- `--rows 3,5` classifies exactly those rows (still freshly, even if a prior
  run already has a record for one of them — there is no resume to skip it),
  errors cleanly on a nonexistent row, and can't be combined with `--n`.

### `test_excel_out.py` — the Deal Profile audit sheet
The output Excel gets a "Deal Profile" tab with the full (uncapped) evidence
when profile data exists, and doesn't when it doesn't.

### `test_invoice_local_resolve.py` — local invoice resolution never guesses
When a row's mined invoice key is resolved against the local PDF library, a
single match resolves, two matches that can't be disambiguated report
**`ambiguous`** (never a guess), and no match stays `no_match`. Also covers the
bare-filename fallback (a hand-named PDF like `INV-12345.pdf` still resolves)
and a control proving that fallback stays inert when the primary path already
resolves.

### `test_quarter_scope.py` — `--quarter` / `--guided` scope derivation, and `ingest.filter_scope`'s `months` parsing
`months_of_quarter` maps a label like `2026Q1` to its three `YYYYMM` months and
rejects malformed labels; the guided/`--quarter` path emits those months **with
a trailing comma** so `filter_scope` reads them as an explicit period list
rather than "the latest N periods" (the all-digit footgun). Also covers
`filter_scope`'s bare-6-digit-YYYYMM rule: a comma-free token like `"202607"`
with a valid month (01-12) is one literal period (not a count), checked
before the plain-count branch; a short digit string like `"6"` still means a
count, and a 6-digit token with an invalid month (e.g. `"100000"`) falls
through to the count branch unchanged.

### `token_extraction_cases.txt` — data file, not a test
A table of `input text => expected result` cases for `invoice_mining` (pulling
invoice numbers out of messy description text), executed by
`test_invariants.py`. Documents tricky cases: e.g. `Deloitte INV8006811344` →
`8006811344`, while dates (`10/18/24`) and hyphenated words (`Multi-Tenant`)
must **not** be mistaken for invoice numbers.

### `snapshot_canon.py` — refactor-proof tool, not a test
Dumps `classified.xlsx` + `summary.json` to deterministic canonical text
(values, fills, hyperlinks; the volatile `generated_at` timestamp is the only
excluded field). Generate a dump before and after a refactor via
`python -m gna_pipeline recover` (zero API cost) and diff — an empty diff is
the proof the refactor changed nothing.
