# Guide — G&A Non-Recurring Classifier

This is the operator walk-through: what to upload, what each screen does, and
what the one file you download at the end contains.

---

## 0. Starting and stopping the app

**To start:** double-click **`Start.cmd`** in the project folder. A terminal
window opens, starts the app, and your browser opens to it automatically (the
first launch takes a minute while it installs what it needs; later launches are
quick). If the browser doesn't open on its own, go to **http://127.0.0.1:8420**.

**The very first launch asks for your Anthropic API key** (paste it into the
terminal window when prompted — it starts with `sk-ant-`). It then does a tiny
one-token check to confirm the key works before opening the app, and saves it so
you're never asked again. If your organization uses OneDrive/SharePoint invoice
links, it also offers to collect the Microsoft Graph tenant and client IDs at
the same time — press Enter to skip those; you can connect from the app later.
Once a key is saved, none of this happens again on later launches.

**That terminal window *is* the app.** Keep it open while you work. The app runs
only on this computer — nothing is exposed to the network or the internet.

**To stop:** close the terminal window (or press **Ctrl+C** in it). You don't
have to stop it between runs — it costs nothing sitting idle. **Closing it is
what clears the workbook and results you uploaded** — the app keeps nothing on
disk after it stops, so closing it when you're done is good practice.

**It also stops on its own** after about **15 minutes of inactivity** — no
clicking, typing, scrolling, or mouse movement — and no run in progress. As
long as you're actually using the screen (including reading and scrolling), it
stays up; a run in progress is never interrupted. If you walk away, it shuts
down and clears your uploaded data automatically. When that happens you'll see
a **"Your session timed out"** screen — that's expected, not an error; just
relaunch to start again.

---

## 1. Upload this quarter's two datasets

The tool starts by asking for exactly two Excel workbooks:

- **G&A workbook** — the multi-tab export, one MRI account per tab.
- **A&T workbook** — the flat Acquisition & Transaction rows.

These must be the two files **provided by accounting for the quarter you're
running** — not a reformatted or hand-edited copy. The pipeline expects this
quarter's export to look the same shape as last quarter's: same tabs, same
headers, same columns.

Drop both files in and the tool flattens them into one clean sheet and runs
three checks — **File format**, **Worksheet structure**, **Expense
records** — before anything downstream touches the data.

**If a check fails**, it almost always means this quarter's export deviated
slightly from the previous quarter's Excel format — a renamed tab, a shifted
column, a changed header row. There are only two ways to fix that:

1. Get accounting to reissue the export matching the prior quarter's layout, or
2. Update the pipeline's ingest code to read the new layout.

Once both workbooks pass, you'll see the row count and move on.

---

## 2. The Run tab

Three optional inputs live here before you configure and run:

- **Additional context** — free-form notes to steer the classifier (e.g. a
  disposition that never posted to the M&A account, a vendor to rule in or
  out, a matter number to watch for). Type it directly or drop a `.txt`,
  `.md`, or `.docx` file. **Hard cap: 2,750 words** — the counter turns red
  as you approach it and won't let you go over.
- **External invoices** — drag and drop already-named invoice PDFs (or a
  whole folder of them) so the classifier can read them directly instead of
  relying only on a scanned-copy link.
- **OneDrive access** — connect if this workbook's invoice links point to
  OneDrive/SharePoint; otherwise you'll need to check the acknowledgment box
  to confirm you understand those links won't be readable.

---

## 3. Configure the run

Click **Configure** to open:

- **Quarter** — pick the quarter you're analyzing. The dropdown shows each
  available quarter's row count and M&A row count, pulled straight from the
  workbook you just uploaded.
- **Minimum USD** — the materiality floor; rows below this absolute dollar
  amount are skipped. Defaults to **$999** if left blank.

Everything else — the `CLOSEGL` system-close rule and the `reclass`
keyword rule — is a fixed, automatic pipeline behavior. It is **not** a
setting in this window; it applies the same way on every run regardless of
what quarter or threshold you pick.

---

## 4. Run it

Clicking **Run** takes you to a forecast — never straight to spending money.
You'll see:

- Row and batch counts for both the deal-profile sweep and the classification
  pass.
- A **cost range** and an estimated **wall-clock time**.
- The spend-rail ceiling (the run aborts on its own if actual cost ever
  exceeds it).

Check the readiness box and click **Proceed** to spend money, or **Cancel**
to back out having spent nothing.

---

## 5. Download the results

When the run finishes, there is exactly **one file to download:
`classified.xlsx`**. Opening it shows:

- **The main ledger** — every original row, reconstructed as values, with the
  classification, reasoning, evidence, basis, and invoice-read status
  (`had_invoice`, `invoice_accessed`, `invoice_pointer`, `invoice_error`)
  appended — because the model sometimes can't read an invoice, and that's
  called out explicitly rather than hidden.
- **Human Review Report** — an auditor-facing view collecting just the
  `human_review` and `non_recurring` rows in one place.
- **Run Summary** — the scorecard: counts by classification, dollar totals by
  currency, invoice-read stats, and actual cost vs. the forecast.
- **Deal Profile** — this quarter's inferred M&A deal vocabulary, built from
  the A&T rows and fed into the rest of the system so the classifier
  recognizes this quarter's deals by name.

### Reading the color coding on the main ledger

- **Yellow** — `human_review`. Read these first; `missing_info` names the
  exact open question.
- **Red** — `non_recurring`. The items that adjust the run-rate.
- **Light green** — `reclass`. A bookkeeping reclassification, caught before
  it ever reaches the model.
- **Gray** — `skipped_negative` or `not_processed`.
- **Unshaded** — `recurring`. The confidently-cleared majority.
- **Light orange** on `had_invoice`/`invoice_error` — an invoice was
  referenced but couldn't be read; the row was still classified on its own
  text, and `invoice_error` says why.
