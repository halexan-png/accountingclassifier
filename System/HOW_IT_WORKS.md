# How It Works

The Guide covers what to click. This covers what happens after you click
it — the mechanics behind each decision the classifier makes.

---

## One worklist, built from two workbooks

Your two source workbooks are combined into a single list of rows. Before
anything else happens to it, the rows that represent a company acquisition
or sale are pulled out and examined on their own. They build a reference
list of known deals — names, matter numbers, invoice numbers, aliases —
that the rest of the run leans on, and that list carries over between runs
rather than starting from nothing each quarter.

Every remaining row is then given one of three verdicts: recurring,
non-recurring, or in need of a person's judgment. The known-deal list, plus
whatever notes you've supplied, informs each of those calls. A row with a
usable invoice is read on its own, invoice included; rows without one are
grouped and classified in batches, since there's no invoice content at
risk of getting crossed with someone else's.

---

## What the model is shown, and in what order

Before it looks at a single row, the classifier is handed five pieces of
background, always in the same sequence:

1. **A one-paragraph description of the business.** Fixed, not editable —
   it establishes that this is a REIT, that the ledger comes from MRI, and
   what a "row" is made of. Nothing about how to judge anything, only what
   the words mean.
2. **The Classifier Doctrine** (`doctrines/classifier.md`, editable in
   Settings). The actual rulebook — the recurring / non-recurring / human
   review test, the questions that get asked in order, what counts as a
   named deal. This is the single source of truth, and it applies to every
   row, every run, without exception.
3. **A statement of which source outranks which**, addressed on its own
   below. Fixed, and deliberately kept out of the doctrine file.
4. **Company Norms** (`data/input/companynorm.md`, editable in Settings,
   optional). Standing notes about this particular company — routine
   vendors, recurring practices — the kind of thing true quarter after
   quarter, which doesn't belong in a general rulebook. Left out entirely
   if the file is empty.
5. **Operator context** (`workspace/user_deal_context.md`, edited by hand
   or through the Additional Context box). Notes for the quarter at hand —
   *row X's advisor belongs to the Redwood deal*, that kind of thing. This
   is where a person tells the model something it has no way of inferring
   on its own.

A sixth item follows, though nobody writes it: the **known-deal index**,
assembled by the model itself during the earlier pass over this quarter's
acquisition and transaction activity, and placed last.

The order isn't arbitrary. It runs from permanent to temporary, general to
specific. The business description comes first because the doctrine
already uses words like "invoice" and "row" as settled facts, so they need
to be settled before it's read. The doctrine comes second because it's the
default outcome for every row, and everything after it only matters where
it stays silent. The authority statement follows immediately, so that by
the time the model reaches company norms and operator notes, it already
knows what weight to give each. Norms precede operator context because
standing knowledge is more stable than a note written for one quarter.

The known-deal index sits last for a reason that has nothing to do with
importance: it's the only block that changes every quarter, so keeping it
at the end lets everything before it be reused, cached, and billed at a
cheaper rate across every batch in the run, rather than reprocessed from
scratch each time. Shown last is not the same as outranking everything —
of the three sources that actually bear on a decision, the known-deal
index carries the least authority. It can only suggest that a name might
belong to a deal; it never settles anything by itself.

---

## Who wins when sources disagree

1. **The Doctrine** decides by default — everything the two sources below
   don't specifically address.
2. **The known-deal index** is a lookup tool, nothing more. It helps the
   model recognize that a name belongs to a known deal, but recognition
   alone never decides an outcome; the doctrine still runs its test.
3. **Operator context** outranks both. It's information a person knows
   that appears nowhere in the ledger or the invoices, so if an operator
   has written an explicit rule and its conditions are met, that rule is
   followed as written, ahead of the doctrine's general test.

In short: the rulebook governs by default, the deal index only helps spot
a name, and a human note overrides both when it applies.

---

## How a single row gets decided

The model works a fixed sequence rather than jumping to a conclusion:

1. Read the row's own text.
2. Read the attached invoice, if there is one — a deal's name can sit in
   the invoice and nowhere in the row's own description.
3. Check any names found against the known-deal index and the operator's
   notes.
4. Answer the doctrine's questions in order, and stop at the first one
   that settles it.
5. Write down the evidence and the reasoning before writing down the
   label — the label has to follow from what's written, not the reverse.

The practical effect: a row that clearly names a real deal is never called
recurring, even when the described work sounds routine. Naming a deal
always raises a row to human review at minimum, and settles it as
non-recurring outright if the match is solid.

---

## Editing the rulebook takes effect immediately

Save an edit to the doctrine, company norms, or operator context, and the
live system picks it up on the very next run — nothing to rebuild or
restart. All three are read from disk at the start of each run; there's no
cached copy sitting in memory between runs. The Settings screen writes
directly to those same files, so editing there is no different from
editing them by hand.

As a safeguard, Settings won't save while a run is in progress — it asks
you to try again once the run finishes. A run reads these files once, at
the start, and holds that copy for its entire length rather than
re-reading row by row, so an edit saved mid-run would land after the copy
was already taken and apply to nothing. Saving between runs is exactly
when it's meant to happen.

One file behaves differently: `workspace/user_deal_context.md`, the
per-quarter deal notes, isn't part of the Settings screen. Edit the file
directly and it behaves like the other two — permanent, picked up
automatically next run. Type into the **Additional Context** box at launch
instead, and you get a one-time override for that run only: it replaces
the file's contents for the run about to happen and is never written back
to disk. It won't be there next quarter, and it won't appear if someone
opens the file later to see what's in it. A note meant to last belongs in
the file; a one-off instruction for the run about to start belongs in the
box.

---

## Matching invoices to a row

Every row either carries a direct link to its invoice, or it doesn't.

If it has a link, that link is what gets used — the system never falls
back to searching the row's description, even if the link turns out to be
broken. A broken link just marks the invoice "not read."

If it has no link, the system searches the description text for an
invoice number instead, checking the main description first and the
additional description only if the main one comes up empty. Three ways of
writing that number are recognized, from most to least reliable:

1. **`INV` beside the number**, with a space, `#`, `-`, `.`, or `:`
   between them — `INV#84512`, `INV-84512`, `INV 84512`. Checked first,
   and the form worth using on purpose.
2. **Leading the description**, followed by at least two spaces before the
   rest of the text — `84512  Legal fees - Project Alpha`.
3. **A bare number beside an ordinary word**, with nothing marking it as
   an invoice number. This still works, but only by chance; don't rely on
   it deliberately.

A few rules apply no matter which form is used. The number must contain at
least one digit — letters alone are never picked up. It must not resemble
a date (`3/14/24` is always ignored). It needs at least four characters
when written with `INV`, or five characters otherwise; anything shorter is
treated as if nothing were written at all, and the row shows as invoice
not read. And because the description field holds only forty characters,
a number placed right at the end of a full field can get silently
truncated — the safest placement is at the very front, with any
overflow moved to the additional description field.

Whatever number appears in the description has to match the invoice's
file name once spacing, punctuation, and case are stripped away — writing
`INV-84512` in the description matches a file named `84512.pdf` just as
well as `INV-84512.pdf`.

Only PDFs are accepted, dropped in one at a time or as a whole folder,
each under a fixed size limit. A separate lookup file can be added to tell
two invoices apart when they'd otherwise share a number, but it's optional
— a correctly named PDF is enough on its own. When a number matches more
than one file and nothing distinguishes them, the invoice is left unread
and flagged as unclear, rather than guessed at.
