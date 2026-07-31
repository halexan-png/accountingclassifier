# Guide — G&A Non-Recurring Classifier

What to upload, what each screen does, and what the one file you download
at the end contains.

---

## 0. Starting and stopping

Double-click **`Start.cmd`** in the project folder. A terminal window opens,
the app starts inside it, and your browser opens to it on its own — the
first launch takes a minute while it installs what it needs; every launch
after that is quick. If the browser doesn't open by itself, go to
**http://127.0.0.1:8420**.

The first launch only, that terminal will ask for your Anthropic API key
(it starts with `sk-ant-`). The app runs a one-token check to confirm the
key works, saves it, and never asks again. If your organization reads
invoice links from OneDrive or SharePoint, it will also offer to collect
the Microsoft Graph tenant and client IDs at the same time — press Enter to
skip that and connect from inside the app later.

That terminal window *is* the app. Leave it open while you work. Nothing
here touches the network or the internet; it runs only on this machine.

To stop, close the terminal window, or press **Ctrl+C** inside it. There's
no need to stop it between runs — it costs nothing sitting idle. Closing it
is what clears whatever you've uploaded: the app keeps nothing on disk once
it stops, which is exactly why closing it when you're done is good
practice.

It will also stop itself after about **fifteen minutes** with no clicking,
typing, scrolling, or mouse movement, and no run underway. Reading the
screen counts as activity, so it won't cut you off mid-thought, and a run
in progress is never interrupted. Walk away for too long and it shuts
down on its own, clearing your uploaded data with it — you'll come back to
a **"Your session timed out"** screen. That's expected, not a fault.
Relaunch and start again.

---

## 1. Upload this quarter's two workbooks

Two Excel files, always:

- **G&A workbook** — the multi-tab export, one MRI account per tab.
- **A&T workbook** — the flat Acquisition & Transaction rows.

These have to be the files accounting issued for the quarter you're
running, unedited. The pipeline expects this quarter's export to take the
same shape as last quarter's — same tabs, same headers, same columns.

Drop both in and the tool flattens them into a single clean sheet, then
runs three checks before anything downstream touches the data: **file
format**, **worksheet structure**, **expense records**. A failure here
almost always means the export drifted slightly from last quarter's layout
— a renamed tab, a shifted column, a header row that moved. There are
exactly two fixes: have accounting reissue the export in the prior layout,
or update the pipeline's ingest code to read the new one.

Once both workbooks clear, you'll see the row count and move on.

---

## 2. The Run tab

Three optional inputs live here, before you configure anything:

- **Additional context** — free-form notes to steer the classifier: a
  disposition that never posted to the M&A account, a vendor to rule in or
  out, a matter number worth watching for. Type it directly, or drop a
  `.txt`, `.md`, or `.docx` file. There's a hard ceiling of **2,750 words**
  — the counter turns red as you approach it and won't let you cross it.
- **External invoices** — drag in already-named invoice PDFs, or a whole
  folder of them, so the classifier reads them directly instead of leaning
  on a scanned-copy link.
- **OneDrive access** — connect it if this workbook's invoice links point
  to OneDrive or SharePoint; otherwise, check the box acknowledging those
  links won't be readable.

---

## 3. Configure the run

Click **Configure** to set two things:

- **Quarter** — the quarter you're analyzing. The dropdown shows each
  available quarter's row count and M&A row count, read straight off the
  workbook you just uploaded.
- **Minimum USD** — the materiality floor. Rows below this dollar amount
  are skipped. Leave it blank and it defaults to **$999**.

Everything else — the `CLOSEGL` system-close rule, the `reclass` keyword
rule — is fixed pipeline behavior, not a setting in this window. It applies
identically on every run, whatever quarter or threshold you choose.

---

## 4. Run it

Clicking **Run** takes you to a forecast, never straight to spending money.
You'll see row and batch counts for both the deal-profile sweep and the
classification pass, a cost range, an estimated wall-clock time, and the
spend-rail ceiling — the run stops itself if actual cost ever crosses it.
Check the readiness box and click **Proceed** to spend money, or
**Cancel** to walk away having spent nothing.

---

## 5. Download the results

When the run finishes, there is exactly one file to download:
**`classified.xlsx`**. Open it and you'll find:

- **The main ledger** — every original row, reconstructed as values, with
  the classification, reasoning, evidence, basis, and invoice-read status
  (`had_invoice`, `invoice_accessed`, `invoice_pointer`, `invoice_error`)
  added on. The model sometimes can't read an invoice; when that happens,
  it says so, rather than pretending otherwise.
- **Human Review Report** — the `human_review` and `non_recurring` rows,
  collected in one place for whoever's auditing the quarter.
- **Run Summary** — the scorecard: counts by classification, dollar totals
  by currency, invoice-read statistics, actual cost against the forecast.
- **Deal Profile** — this quarter's inferred M&A vocabulary, built from the
  A&T rows and fed back into the rest of the system so the classifier
  recognizes this quarter's deals by name.

### Reading the color coding

- **Yellow** — `human_review`. Read these first; `missing_info` names the
  exact open question.
- **Red** — `non_recurring`. The items that move the run-rate.
- **Light green** — `reclass`. A bookkeeping reclassification, caught
  before it ever reaches the model.
- **Gray** — `skipped_negative` or `not_processed`.
- **Unshaded** — `recurring`. The confidently-cleared majority.
- **Light orange**, on `had_invoice`/`invoice_error` — an invoice was
  referenced but couldn't be read. The row was still classified on its own
  text, and `invoice_error` says why.
