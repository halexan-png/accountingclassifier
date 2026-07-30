# Intended input file

Two workbooks are uploaded (G&A slot, A&T slot). Everything below is exact —
cell positions, header text, matching rules — as implemented in
`gna_pipeline/flatten_q2.py` and `gna_pipeline/ingest.py`.

Sample empty templates: `memo/sample_ga_workbook.xlsx`, `memo/sample_at_workbook.xlsx`.

---

## 1. G&A workbook (upload slot "G&A")

One worksheet tab **per MRI account**. Each tab, independently:

| Cell / row | Required value |
|---|---|
| `A1` | Account name, free text (e.g. `Legal Fees`) — used as the `Category` and `Source Tab` output value. If blank, the tab's own sheet name is used instead. |
| `A2` | MRI account number, must match `^MR\d+` (e.g. `MR70000000`). **A tab whose A2 does not match this pattern is skipped entirely — no error, no rows extracted.** |
| header row | Any single row within rows **1–12** whose cells, taken together, contain the substrings `period`, `counterparty`, and `amount` (case-insensitive, one match per token, any cell). Row 6 is the conventional position. If no row in 1–12 satisfies all three tokens, row 6 is silently assumed as the header — this can misread the whole tab if row 6 isn't really the header. |
| data rows | Start immediately below the header row. A row whose `period` cell is blank is dropped silently (not an error). |

Header columns are matched by **text (case-insensitive substring), not fixed position** — reorder freely. Required/consumed header text:

| Header must contain | Field | Reaches output? |
|---|---|---|
| `period` | period | yes — `PERIOD` |
| `counterparty` | counterparty | no — needed only to auto-detect the header row |
| `amount` | amount | yes — `AMT` and `USD Amount` |
| `date` | date | yes — `ENTRDATE` |
| `quarter` | quarter | yes — `Quarter` |
| `invoice link` | invoice_link | yes — `Image URL - Hyperlink` |
| `page` | page | no — detection/context only |
| `type` | type | no — detection/context only |
| `userid` | userid | yes — `USERID` |
| `descrpn` or `description` | descrptn | yes — `DESCRPN` |
| `addldesc` or `adddesc` | adddesc | yes — `ADDLDESC` |

A tab missing one of these header columns does not error — that field is written blank for every row on the tab, silently.

Currency is not read from this workbook: every G&A row is written with `OCURRCODE = USD`, `Exchange Rate = 1`.

---

## 2. A&T workbook (upload slot "A&T")

One flat sheet, header on **row 1** (no detection tolerance — row 1 only).

Sheet selection: a sheet literally named `Acquisition & Transaction 2026`
(case-insensitive) is used if present; otherwise the **first** sheet anywhere
in the file with a header cell exactly equal to `USD Amount` (case-insensitive)
is used instead.

Header row must contain these 20 exact strings (case-insensitive **exact
match**, not substring — reorder freely, extra columns are ignored, but a
column worded even slightly differently is not recognized and comes out
blank, silently, with no warning):

```
PERIOD  REF  SOURCE  ENTITYID  ACCTNUM  DEPARTMENT  AMT  DESCRPN  PDENTRY
ENTRDATE  OCURRCODE  ADDLDESC  LASTDATE  USERID  Category  Quarter
Image URL  Image URL - Hyperlink  Exchange Rate  USD Amount
```

Row inclusion: only rows where `ACCTNUM` equals `MR58200000` (or its
digit-only form `58200000`) are kept. Every other row — other accounts,
thousands of blank trailing rows — is dropped automatically, silently, no
error either way. **If the ACCTNUM header text doesn't match exactly, every
row is excluded and the tab contributes zero rows with no warning at all.**

Currency here is native: `OCURRCODE` / `Exchange Rate` are passed through
as-is (not forced to USD/1).

---

## 3. Merged canonical file (internal — what the pipeline actually reads)

The two uploads are merged into one flat single-sheet workbook (sheet name
`G&A MRI Records - With Link`) in this exact column order — this is also the
required layout if a single already-flattened file is uploaded directly:

```
1 PERIOD    2 REF     3 SOURCE      4 ENTITYID    5 ACCTNUM
6 DEPARTMENT 7 AMT     8 DESCRPN     9 PDENTRY     10 ENTRDATE
11 OCURRCODE 12 ADDLDESC 13 LASTDATE 14 USERID     15 Category
16 Quarter   17 Image URL 18 Image URL - Hyperlink 19 Exchange Rate
20 USD Amount 21 Source Tab
```

Of these, the pipeline consumes 14 fields via case-insensitive substring
alias match (reorder-safe); if a field's alias can't be found anywhere in
row 1, it falls back to reading the fixed column index below (garbage in,
no error) — this fallback table only matters for a hand-built single-file
upload, not the two-slot flow, since the merge step above always writes
these headers verbatim:

| Field (→ `RowPacket`) | Header alias(es) | Fallback column |
|---|---|---|
| period | `period` | 1 |
| ref | `ref` | 2 |
| source | `source` | 3 |
| entityid | `entityid`, `entity id` | 4 |
| acctnum | `acctnum`, `account number`, `acct` | 5 |
| department | `department`, `dept` | 6 |
| descrptn | `descrpn`, `description`, `memo` | 8 |
| entrdate | `entrdate`, `entry date` | 10 |
| currency | `ocurrcode`, `currcode`, `currency` | 11 |
| adddesc | `addldesc`, `adddesc` | 12 |
| userid | `userid`, `user id` | 14 |
| category | `category` | 15 |
| invoice_url | `image url - hyperlink`, `hyperlink`, `scannedcopyurl`, `invoice url` | 18 |
| amount | `usd amount`, `usdamt` — **must resolve to a header reading exactly `usd amount`**, any other match is flagged as a defect | 20 |

These 14 fields (`RowPacket`) are the only row data any downstream stage —
classification, invoice matching, the Excel output, the Human Review Report
tab — ever sees. Nothing else in the workbook is read past this point.
