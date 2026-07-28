# Workspace

This folder is the only place you need to touch to run a quarter's G&A
non-recurring classification. Everything else in this repo is the program
itself.

## What goes here

1. **Your source workbook** — drop the G&A records `.xlsb` file straight
   into this folder (e.g. `workspace/G&ARecordsWLink.xlsb`). Keep exactly
   one `.xlsb` file here; the pipeline finds it automatically. If it can't
   find one, or finds more than one, it will tell you and stop before
   spending anything.

2. **`user_deal_context.md`** — this quarter's deal notes, in your own
   words. See the comment block at the top of that file for what to write.
   (Renamed 2026-07-17 from `deal_context.md`; the pipeline still reads an
   old-named file if present, with a one-time warning.)

## What comes back

Results land in `results/` inside this workspace:

- `results/classified.xlsx` — the annotated G&A sheet (every row's
  classification, basis, reasoning, evidence, and flags, color-coded), plus
  a "Run Summary" scorecard sheet and a "Deal Profile" sheet.
- `results/summary.json` — the same run, as machine-readable numbers.

Both files are rebuilt every time you run the pipeline (`run`) or recover
one at zero API cost (`recover`) — you never need to move or rename
anything by hand.

## Why data is loaded, not overwritten

Nothing you drop here is ever modified by the pipeline. It reads your
workbook and your deal notes, and only ever writes into `results/`.

## Deal context — the details

`user_deal_context.md` is the one place to tell the model about deals it can't
discover on its own from the M&A account rows alone — a disposition that
never posted there, a vendor name to explicitly rule in or out, a matter
number to watch for. It is treated as **higher authority** than whatever
the automated sweep infers.

- Write free-form prose — there's no required format.
- There's a **2,500-word hard cap**. Past it, every command refuses to run
  until you trim the file (a large file gets sent on every batch of every
  paid API call, so this is a real cost control, not a formality). You'll
  get a heads-up once you cross ~1,250 words so you notice before it
  becomes a problem.
- An empty or missing file is completely normal — it just means no extra
  context is added this quarter.
