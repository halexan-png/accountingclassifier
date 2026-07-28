"""invoice_mining.py — mine invoice keys from row text + resolve them against the
local invoice directory.

`normalize()` is byte-identical to `externalinvoices/make_by_invoice_no.py::normalize`
(that script materializes the per-invoice PDFs into `externalinvoices/` and defines
the lookup contract this module honors — see its module docstring).

Order of operations:
    1. Scan `descrptn`, then `adddesc`, with three patterns in priority order:
       (a) an `(?i)\\bINV[\\s#\\-\\.:]*(...)` prefixed token,
       (b) a leading, space-padded token (MRI pads a leading invoice code, e.g.
           "6757459         Womble - Project"),
       (c) a standalone alnum run >=5 chars sitting next to vendor-ish (alphabetic)
           text.
    Date-shaped candidates (`^\\d{1,2}/\\d{1,2}/\\d{2,4}$`) and digit-free tokens
    are rejected at every step — they are never invoice keys.

`load_lookup_index` / `resolve_local` mirror the lookup contract documented at the
top of `make_by_invoice_no.py`: `glob(f"{config.INVOICE_DIR}/{normalize(inv_id)}*.pdf")`
(config.INVOICE_DIR is `externalinvoices/` — the per-invoice PDFs live directly in it).
"""

from __future__ import annotations

import csv
import functools
import logging
import re
from pathlib import Path

from gna_pipeline.contract import RowPacket

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# normalize() — byte-identical to externalinvoices/make_by_invoice_no.py::normalize
# ---------------------------------------------------------------------------
_NORMALIZE_INV_PREFIX_RE = re.compile(r"^INV[\s#\-\.:]*")
_NORMALIZE_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]")


def normalize(s: object) -> str:
    """Uppercase -> strip a leading INV[#-.:]* prefix -> drop all non-alphanumerics."""
    text = _NORMALIZE_INV_PREFIX_RE.sub("", str(s).upper())
    return _NORMALIZE_NON_ALNUM_RE.sub("", text)


# ---------------------------------------------------------------------------
# 4.1 Token extraction
# ---------------------------------------------------------------------------

# (a) INV-prefixed token: separator chars after INV are optional and consumed,
# the captured group is the candidate key (still un-normalized).
_INV_PREFIX_RE = re.compile(r"(?i)\bINV[\s#\-\.:]*([A-Z0-9][A-Z0-9\-\/]{3,})")

# (b) MRI pads a leading invoice code with 2+ spaces before the description text.
_LEADING_TOKEN_RE = re.compile(r"(?i)^([A-Z0-9][A-Z0-9\-\/\.]{4,})\s{2,}")

# (c) a standalone alnum run >=5 chars, checked token-by-token against its
# whitespace-delimited neighbors for "vendor-ish" (alphabetic, len>=3) context.
_STANDALONE_TOKEN_RE = re.compile(r"(?i)^[A-Z0-9][A-Z0-9\-\/]{4,}$")
_ALPHA_WORD_RE = re.compile(r"^[A-Za-z]{3,}$")
_WORD_PUNCT_STRIP = ".,;:()[]\"'"

# Reject filters shared by all three patterns.
_DATE_SHAPED_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")

# MRI's `descrpn` field is capped at 40 chars; a mined token whose match runs
# flush to the end of a field at/over that length may have been cut off mid-token.
_MRI_DESCRPTN_TRUNCATION_LEN = 40


def _rejected(token: str) -> bool:
    """Date-shaped or digit-free candidates are never invoice keys. The digit
    requirement subsumes the old isalpha() check, which missed hyphenated word
    tokens — 'Multi-Tenant' mined as key MULTITENANT and flagged had_invoice
    on plain bank-fee rows."""
    if _DATE_SHAPED_RE.match(token):
        return True
    if not any(ch.isdigit() for ch in token):
        return True
    return False


def _is_truncated(raw_text: str, match_end_idx: int) -> bool:
    return len(raw_text) >= _MRI_DESCRPTN_TRUNCATION_LEN and match_end_idx >= len(raw_text)


def _find_standalone_token(text: str) -> tuple[str | None, int]:
    """Pattern (c): a standalone alnum run >=5 chars adjacent to vendor-ish text."""
    words = text.split()
    if not words:
        return None, 0

    # Track each word's start offset in `text` so we can report a match end
    # index for the truncation check.
    offsets: list[int] = []
    cursor = 0
    for w in words:
        start = text.index(w, cursor)
        offsets.append(start)
        cursor = start + len(w)

    def _core(word: str) -> str:
        return word.strip(_WORD_PUNCT_STRIP)

    for i, word in enumerate(words):
        core = _core(word)
        if len(core) < 5 or not _STANDALONE_TOKEN_RE.match(core):
            continue
        if _rejected(core):
            continue
        prev_core = _core(words[i - 1]) if i > 0 else ""
        next_core = _core(words[i + 1]) if i < len(words) - 1 else ""
        if _ALPHA_WORD_RE.match(prev_core) or _ALPHA_WORD_RE.match(next_core):
            end_idx = offsets[i] + len(core)
            return core, end_idx

    return None, 0


def _scan_text(text: str, *, is_descrptn: bool) -> tuple[str | None, bool]:
    """Try patterns (a), (b), (c) in priority order over one field's text."""
    m = _INV_PREFIX_RE.search(text)
    if m and not _rejected(m.group(1)):
        return m.group(1), is_descrptn and _is_truncated(text, m.end(1))

    m = _LEADING_TOKEN_RE.match(text)
    if m and not _rejected(m.group(1)):
        return m.group(1), is_descrptn and _is_truncated(text, m.end(1))

    token, end_idx = _find_standalone_token(text)
    if token is not None:
        return token, is_descrptn and _is_truncated(text, end_idx)

    return None, False


def mine_invoice_key(packet: RowPacket) -> tuple[str | None, bool]:
    """Mine an invoice key from a RowPacket.

    Returns (normalized_key_or_None, truncated). `descrptn` then `adddesc` are
    scanned with the section 4.1 patterns. `truncated` is only ever True for a
    `descrptn` match (the field MRI caps at 40 chars) whose raw token ran
    flush to the end of that field.
    """
    for field_name, is_descrptn in (("descrptn", True), ("adddesc", False)):
        text = packet.get(field_name)
        if not text:
            continue
        raw_token, truncated = _scan_text(text, is_descrptn=is_descrptn)
        if raw_token is not None:
            return normalize(raw_token), truncated

    return None, False


# ---------------------------------------------------------------------------
# 4.2 Local directory resolution
# ---------------------------------------------------------------------------

_LOOKUP_FIELDS = (
    "vendor_invoice_no",
    "invoice_key",
    "gnl_ref",
    "pdf_file",
    "pages",
    "gl_refs",
    "gl_entityids",
    "found_in_gl",
)


def load_lookup_index(csv_path: str | Path) -> dict[str, list[dict]]:
    """Index invoice_lookup.csv by normalize(invoice_key); also index
    normalize(vendor_invoice_no) when it differs from the invoice_key's
    normalized form, so either identifier can resolve a row."""
    index: dict[str, list[dict]] = {}
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            invoice_key = (row.get("invoice_key") or "").strip()
            vendor_invoice_no = (row.get("vendor_invoice_no") or "").strip()
            entry = {field: row.get(field, "") for field in _LOOKUP_FIELDS}
            entry["pages"] = entry["pages"] or None

            norm_key = normalize(invoice_key) if invoice_key else None
            if norm_key:
                index.setdefault(norm_key, []).append(entry)

            if vendor_invoice_no:
                norm_vendor = normalize(vendor_invoice_no)
                if norm_vendor and norm_vendor != norm_key:
                    index.setdefault(norm_vendor, []).append(entry)

    return index


def _entityid_matches(entityid: str, gl_entityids: str) -> bool:
    if not gl_entityids:
        return False
    parts = [p.strip() for p in re.split(r"[;,]", gl_entityids) if p.strip()]
    return entityid.strip() in parts


def _matching_row(pdf_path: Path, index: dict[str, list[dict]]) -> dict | None:
    """Find the single CSV row behind a local PDF path.

    make_by_invoice_no.py renames files to "{invoice_key}.pdf", or
    "{invoice_key}__{gnl_ref}.pdf" when one invoice_key spans multiple CSV
    rows/packets — it does NOT keep the CSV's original `pdf_file` name. So:
    look the rows up by the candidate's OWN filename-embedded key (not
    whatever possibly-truncated search key produced the glob hit); a
    single-row key is unambiguous, a multi-row key is disambiguated by the
    gnl_ref suffix baked into the filename.
    """
    true_key, sep, gnl_ref = pdf_path.stem.partition("__")
    rows = index.get(true_key, [])
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    if sep:
        for row in rows:
            if row.get("gnl_ref") == gnl_ref:
                return row
    return None


@functools.lru_cache(maxsize=None)
def _normalized_dir_index(invoice_dir: str) -> dict[str, list[Path]]:
    """Index every PDF directly under `invoice_dir` by normalize(stem), cached
    per directory so the bare-filename fallback below (see resolve_local)
    globs the directory once per run rather than once per row.

    This indexes the RAW FILENAME, unlike `load_lookup_index` which indexes
    the CSV's `invoice_key`/`vendor_invoice_no` columns. It's what lets an
    operator drop invoice PDFs named by invoice number (`1231239.pdf`,
    `INV-12345.pdf`) straight into `invoice_dir` with no invoice_lookup.csv
    present at all, as long as the filename normalizes to the same key a row
    mines.
    """
    index: dict[str, list[Path]] = {}
    directory = Path(invoice_dir)
    if not directory.is_dir():
        return index
    for pdf_path in directory.glob("*.pdf"):
        file_key = normalize(pdf_path.stem)
        if file_key:
            index.setdefault(file_key, []).append(pdf_path)
    return index


def _resolve_via_filename_fallback(
    key: str,
    directory: Path,
    index: dict[str, list[dict]],
) -> tuple[Path | None, str | None, str]:
    """Fallback tried only after the primary exact/prefix-glob resolution in
    resolve_local() finds zero candidates: normalize every PDF filename in
    `directory` and compare to the mined `key`. Single hit resolves (whole
    PDF, no CSV row required); multiple hits are reported as "ambiguous",
    never guessed; no hit leaves the row "no_match".
    """
    hits = _normalized_dir_index(str(directory)).get(key, [])
    if not hits:
        return None, None, "no_match"
    if len(hits) > 1:
        logger.warning(
            "invoice key %r ambiguous via filename fallback: %d candidate PDFs",
            key,
            len(hits),
        )
        return None, None, "ambiguous"
    p = hits[0]
    row = _matching_row(p, index)
    return p, (row.get("pages") or None) if row else None, "resolved"


def resolve_local(
    key: str | None,
    truncated: bool,
    entityid: str | None,
    invoice_dir: str | Path,
    index: dict[str, list[dict]],
) -> tuple[Path | None, str | None, str]:
    """Resolve a mined key to a local PDF.

    Lookup order: exact "{key}.pdf" first (works even when `truncated`, in the
    rare case the truncated token happens to equal a full filename); then a
    prefix glob "{key}*.pdf" — the ONLY option once `truncated` is True, since
    the trailing characters of the true key were never captured. If that
    finds nothing, `_resolve_via_filename_fallback` gets one more try — see
    its docstring — before the row is finally "no_match".

    Multiple hits are tie-broken by `entityid` against the CSV rows' `gl_entityids`
    (join key = entityid + normalized token). The spec's second tie-break ("row
    amount in the CSV context") is unimplementable: invoice_lookup.csv carries no
    amount column. Any remaining ambiguity is reported, never guessed.

    `truncated` is accepted (not just inferred from `key`) so callers stay the
    single source of truth for that flag from mine_invoice_key(); it does not
    otherwise change the lookup order above, which already tries exact-then-glob
    regardless.
    """
    if not key:
        return None, None, "no_match"

    directory = Path(invoice_dir)

    exact = directory / f"{key}.pdf"
    candidates = [exact] if exact.exists() else sorted(directory.glob(f"{key}*.pdf"))

    if not candidates:
        return _resolve_via_filename_fallback(key, directory, index)

    if len(candidates) == 1:
        p = candidates[0]
        row = _matching_row(p, index)
        return p, (row.get("pages") or None) if row else None, "resolved"

    if entityid:
        matched = []
        for p in candidates:
            row = _matching_row(p, index)
            if row and _entityid_matches(entityid, row.get("gl_entityids", "")):
                matched.append(p)
        if len(matched) == 1:
            p = matched[0]
            row = _matching_row(p, index)
            return p, (row.get("pages") or None) if row else None, "resolved"

    logger.warning(
        "invoice key %r ambiguous: %d candidate PDFs, entityid tie-break did not resolve",
        key,
        len(candidates),
    )
    return None, None, "ambiguous"
