"""invoice_read.py — read an invoice document into an InvoiceResult.

Ports `phase2/invoices.py` WHOLE for the URL source: the same urllib fetch loop with
exponential-backoff retry on transient failures, the same login-page/HTML rejection,
the same `%PDF-` magic-byte check and oversize handling. Adds a local-file source, pypdf
text extraction with a vision fallback, and conservative token estimation on top.

Phase 0 *acquires* the document at $0 (no API call) — fetches the URL bytes or loads the
local PDF, extracts text / counts pages, records size + sha256 — so Phase-2 batch sizing
has real token counts. The token *spend* (attaching the document to a model
request) happens later, in Phase 2.

Trust boundary (ported, unchanged in spirit):
  - Only a `%PDF-`-verified body is treated as a document; only text/plain, application/json,
    or other text/* bodies are treated as an inline text invoice. text/html is always
    kind="error" — very likely an AvidXchange login or expired-link page, not the invoice.
    A decoded text body is additionally scanned for login markers; if found, kind is forced
    to "error". This pushes ambiguous rows to a null-invoice / human_review outcome instead
    of feeding the model a confident wrong answer.
  - The transport call is retried with exponential backoff on transient failures (timeouts,
    connection resets, 5xx). HTTP 4xx (except 408 and 429) are permanent and not retried.
  - Oversize (> config.INVOICE_MAX_BYTES) is always an error — never truncated.

Read-path selection (new in this module): every PDF page range is packaged as a native
PDF document block for a vision request (see _read_pdf) — never bare pypdf text. Page
SELECTION (see _select_pages) is windowed: the window is the whole document, or a parsed
CSV page-range hint clamped to the file's real page count. A window of
config.PAGE_FULL_READ_MAX pages or fewer is read in full; a longer window reads only its
first + last config.PAGE_EDGE_COUNT pages — the middle is skipped, and was_truncated
below detects the gap this leaves in pages_read.

Deviation: phase2/invoices.py truncated inline text bodies to a configured
`invoice_text_chars` cap. gna_pipeline/config.py defines no such constant, so this module
does not truncate text bodies — est_input_tokens is computed off the full text so the token
estimate stays honest rather than silently capping an unbounded value.
"""

from __future__ import annotations

import hashlib
import io
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal, TypedDict

from pypdf import PdfReader, PdfWriter

from gna_pipeline import config, graph_auth, graph_fetch
from gna_pipeline.contract import InvoiceSummary

logger = logging.getLogger("gna.invoice")

# Substrings that betray a login / auth-wall page masquerading as an invoice body
# (ported verbatim from phase2/invoices.py).
_LOGIN_MARKERS = (
    "<title>sign in", "sign in", "log in", "login",
    "authentication required", "session expired", "please log in",
    "access denied", "single sign-on", "sso", "forgot password",
)

# HTTP status codes that are worth retrying even though they are 4xx.
_RETRYABLE_4XX = {408, 429}


class InvoiceResult(TypedDict, total=False):
    kind: Literal["pdf", "text", "none", "error"]
    source: Literal["url", "local_file"] | None
    path_or_url: str | None
    text: str | None                 # extracted PDF text (kept for est/cross-checks,
                                     # NOT sent as a block) OR a fetched text body
    pdf_bytes: bytes | None          # the native PDF document block sent to the model;
                                     # set for every PDF (read_path == "vision"), None
                                     # for a fetched inline text body (read_path == "text")
    pages_read: str | None           # e.g. "1-4" — the range ACTUALLY read
    page_count: int | None           # total pages in the source document
    sha256: str | None
    size_bytes: int | None
    read_path: Literal["text", "vision"] | None
    est_input_tokens: int            # conservative HIGH end
    error: str | None


# ---------------------------------------------------------------------------
# Ported trust-boundary helpers (phase2/invoices.py)
# ---------------------------------------------------------------------------

def _looks_like_login(body: str) -> bool:
    low = body.lower()
    return any(marker in low for marker in _LOGIN_MARKERS)


def _is_transient_http(code: int) -> bool:
    return code in _RETRYABLE_4XX or 500 <= code <= 599


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------

def null_invoice() -> InvoiceResult:
    """No invoice referenced at all — never fetched/read."""
    return {
        "kind": "none", "source": None, "path_or_url": None,
        "text": None, "pdf_bytes": None, "pages_read": None, "page_count": None,
        "sha256": None, "size_bytes": None, "read_path": None,
        "est_input_tokens": 0, "error": None,
    }


def _error_result(
    source: Literal["url", "local_file"],
    path_or_url: str,
    error_msg: str,
    *,
    size_bytes: int | None = None,
    sha256: str | None = None,
) -> InvoiceResult:
    return {
        "kind": "error", "source": source, "path_or_url": path_or_url,
        "text": None, "pdf_bytes": None, "pages_read": None, "page_count": None,
        "sha256": sha256, "size_bytes": size_bytes, "read_path": None,
        "est_input_tokens": 0, "error": error_msg,
    }


def unavailable_invoice(
    source: Literal["url", "local_file"], path_or_url: str, error_msg: str
) -> InvoiceResult:
    """Error result for a row that REFERENCED an invoice but no readable
    document could be produced (fetch failed, local lookup missed/ambiguous).
    Callers keep this on the WorkItem/record so the reason survives into
    results.jsonl instead of being dropped with a bare None."""
    return _error_result(source, path_or_url, error_msg)


def downgrade_to_error(res: dict, reason: str) -> InvoiceResult:
    """Downgrade a (vision) InvoiceResult to an `error` result carrying `reason`,
    preserving the identity fields (source/path_or_url/sha256/size_bytes) so a
    record still names WHICH invoice failed. Drops pdf_bytes/text/read_path so
    the item no longer routes to the vision path.

    Used for the vision -> text/row-only fallback: when a native-PDF vision API
    call fails (timeout, 400 on the re-serialized PDF, overload), the row is
    re-attempted with this downgraded invoice, so config.model_for_batch routes
    it to the floor model and prompts._invoice_content_blocks emits a 'could not
    retrieve' notice instead of a PDF block — the model then decides on the
    row's own information."""
    source = res.get("source") or "url"
    return _error_result(
        source, res.get("path_or_url") or "", reason,
        size_bytes=res.get("size_bytes"), sha256=res.get("sha256"),
    )


# ---------------------------------------------------------------------------
# Page-range resolution + pypdf extraction (shared by URL and local sources)
# ---------------------------------------------------------------------------

def _parse_pages_range(pages: str) -> tuple[int, int] | None:
    try:
        a_str, b_str = pages.split("-", 1)
        return int(a_str.strip()), int(b_str.strip())
    except (ValueError, AttributeError):
        return None


def _select_pages(page_count: int, pages: str | None) -> list[int]:
    """1-based page numbers to actually read, sorted and deduped.

    First resolves the WINDOW: (1, page_count) normally, or the parsed CSV
    hint range clamped to [1, page_count] when `pages` parses and falls
    inside the document; an unparsable or out-of-range hint drops through to
    the whole-document window instead.

    Then decides which pages of that window to read: a window of
    config.PAGE_FULL_READ_MAX pages or fewer is read in full; a longer
    window reads only its first + last config.PAGE_EDGE_COUNT pages — the
    middle is skipped (see was_truncated).
    """
    start, end = 1, page_count
    if pages:
        parsed = _parse_pages_range(pages)
        if parsed is not None:
            p_start, p_end = parsed
            p_start = max(1, p_start)
            p_end = min(p_end, page_count)
            if p_start <= page_count and p_start <= p_end:
                start, end = p_start, p_end

    span = end - start + 1
    if span <= config.PAGE_FULL_READ_MAX:
        return list(range(start, end + 1))

    edge = config.PAGE_EDGE_COUNT
    return sorted(set(range(start, start + edge)) | set(range(end - edge + 1, end + 1)))


def _format_pages(pages_list: list[int]) -> str:
    """Compact multi-range string for a sorted, deduped page-number list,
    e.g. [1, 2, 24, 25] -> "1-2,24-25". A contiguous run collapses to
    "A-B"; a lone page is still emitted as "N-N" (not bare "N") so every
    downstream parser (was_truncated, scheduling._parse_pages_read) can
    treat every comma-separated part as an "A-B" range with no singleton
    special case."""
    if not pages_list:
        return ""
    ranges: list[str] = []
    start = prev = pages_list[0]
    for p in pages_list[1:]:
        if p == prev + 1:
            prev = p
            continue
        ranges.append(f"{start}-{prev}")
        start = prev = p
    ranges.append(f"{start}-{prev}")
    return ",".join(ranges)


def _read_pdf(
    raw: bytes, pages: str | None
) -> tuple[str | None, bytes | None, str, Literal["text", "vision"], int, int]:
    """Package the capped page range as a native PDF document block for the model.

    Returns (text, pdf_bytes, pages_read, read_path, page_count, est_input_tokens).

    Every PDF page range is now sent as a native PDF document block (read_path
    == "vision"), unconditionally — the old aggregate mean-chars decision that
    routed "text-heavy" documents to a pypdf-text-only block is gone. The API
    renders each page to an image AND extracts its text layer from the same
    document block, so the model sees layout, stamps, signatures, and fine print
    together with the exact character sequences — strictly more than pypdf's
    linearized text alone, which silently dropped structure and everything not
    in the text layer. This also removes the mixed-fidelity failure mode: a
    mostly-text file with one scanned/stamped page no longer averages that page
    away, because every page is rendered regardless of its char count.

    pypdf's text is still extracted here, but only for (a) the token estimate
    and (b) keeping the extracted text on the in-memory InvoiceResult for
    downstream cross-checks; it is NOT emitted as a separate prompt block (the
    document block already carries the text layer — see
    prompts._invoice_content_blocks). MIN_CHARS_PER_PAGE is no longer consulted.

    Page selection (see _select_pages) reads every page of a
    config.PAGE_FULL_READ_MAX-or-shorter window, or only the first + last
    config.PAGE_EDGE_COUNT pages of a longer one — pages_read is the compact
    multi-range string of whichever pages were actually selected (see
    _format_pages), e.g. "1-2,24-25" for an edge-only read.

    est is the HIGH end (matching the spend-rail philosophy): the native PDF
    block bills BOTH per-page image tokens and the extracted text tokens, so the
    estimate sums both. scheduling.low_end_tokens mirrors this on the low end.
    """
    reader = PdfReader(io.BytesIO(raw))
    page_count = len(reader.pages)
    pages_list = _select_pages(page_count, pages)
    selected = [reader.pages[i - 1] for i in pages_list]
    text = "\n".join((p.extract_text() or "") for p in selected)
    pages_read = _format_pages(pages_list)

    writer = PdfWriter()
    for p in selected:
        writer.add_page(p)
    buf = io.BytesIO()
    writer.write(buf)

    est = len(pages_list) * config.VISION_TOKENS_PER_PAGE[1] + len(text) // config.CHARS_PER_TOKEN
    return text, buf.getvalue(), pages_read, "vision", page_count, est


# ---------------------------------------------------------------------------
# URL source (ported phase2/invoices.py::fetch_invoice, whole)
# ---------------------------------------------------------------------------

def fetch_invoice_url(url: str) -> InvoiceResult:
    """Fetch one invoice URL into an InvoiceResult. Never raises."""
    try:
        return _fetch_invoice_url(url)
    except Exception as e:  # noqa: BLE001 — a reader must never raise
        return _error_result("url", url or "", f"unexpected_error: {e}")


def _fetch_invoice_url(url: str) -> InvoiceResult:
    if not url or url.strip().lower() in ("none", ""):
        return null_invoice()

    # OneDrive/SharePoint links: an anonymous GET on these returns a login
    # page, not the file (see html_response_possible_login_or_expired_url
    # below) — Graph is the only way to actually read them. A missing token
    # is a dead end for this host, not a cue to fall through to the plain
    # anonymous path: that path sends the raw, unencoded SharePoint URL
    # straight to urllib, which throws a misleading low-level error
    # ("URL can't contain control characters") that has nothing to do with
    # the real problem (auth). Fail clearly instead.
    if graph_fetch.is_graph_url(url):
        token = graph_auth.get_token_silent()
        if token:
            raw, content_type, err = graph_fetch.fetch_via_graph(
                url, token, timeout_s=config.INVOICE_TIMEOUT_S
            )
            if raw is None:
                return _error_result("url", url, err or "graph_fetch_failed")
            return _classify_fetched_body(url, raw, content_type)
        reason = (
            "graph_not_configured: GRAPH_TENANT_ID/GRAPH_CLIENT_ID not set for this run"
            if not config.graph_configured()
            else "graph_not_connected: no cached token; reconnect Graph"
        )
        return _error_result("url", url, reason)

    raw: bytes | None = None
    content_type = ""
    last_error = "fetch_failed: unknown"

    # Finding C (ported) — retry transient transport failures with exponential backoff.
    for attempt in range(config.INVOICE_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (compatible; GNL-Classifier/2.0)"},
            )
            with urllib.request.urlopen(req, timeout=config.INVOICE_TIMEOUT_S) as resp:
                content_type = (resp.headers.get("Content-Type", "") or "").lower()
                raw = resp.read(config.INVOICE_MAX_BYTES + 1)
            break
        except urllib.error.HTTPError as e:
            last_error = f"http_{e.code}_{e.reason}"
            if not _is_transient_http(e.code) or attempt == config.INVOICE_RETRIES:
                return _error_result("url", url, last_error)
        except Exception as e:  # noqa: BLE001 — timeouts, conn resets, DNS, etc. are transient
            last_error = f"fetch_failed: {e}"
            if attempt == config.INVOICE_RETRIES:
                return _error_result("url", url, last_error)
        time.sleep(min(2 ** attempt, 30))  # 1s, 2s, 4s, ... capped at 30s

    if raw is None:  # defensive — should be unreachable
        return _error_result("url", url, last_error)

    return _classify_fetched_body(url, raw, content_type)


def _classify_fetched_body(url: str, raw: bytes, content_type: str) -> InvoiceResult:
    """Shared trust-boundary classification for a fetched body, regardless of
    transport (plain anonymous GET or Graph on a delegated token): oversize
    check, PDF magic-byte/content-type sniff, HTML/login-page rejection, and
    the text/* allowlist. Identical rules either way — Graph adds a way to
    FETCH the bytes, never a different rule for what counts as readable."""
    if len(raw) > config.INVOICE_MAX_BYTES:
        return _error_result(
            "url", url, f"oversize_>{config.INVOICE_MAX_BYTES}_bytes", size_bytes=len(raw)
        )

    sha256 = hashlib.sha256(raw).hexdigest()
    size_bytes = len(raw)

    is_pdf = ("pdf" in content_type) or raw[:5] == b"%PDF-"
    if is_pdf:
        try:
            text, pdf_bytes, pages_read, read_path, page_count, est = _read_pdf(raw, pages=None)
        except Exception as e:  # noqa: BLE001 — corrupt/encrypted PDF etc.
            return _error_result(
                "url", url, f"pdf_parse_failed: {e}", size_bytes=size_bytes, sha256=sha256
            )
        logger.info(
            "invoice read: source=url read_path=%s pages_read=%s path_or_url=%s",
            read_path, pages_read, url,
        )
        return {
            "kind": "pdf", "source": "url", "path_or_url": url,
            "text": text, "pdf_bytes": pdf_bytes, "pages_read": pages_read,
            "page_count": page_count, "sha256": sha256, "size_bytes": size_bytes,
            "read_path": read_path, "est_input_tokens": est, "error": None,
        }

    # P4 (ported) — HTML is NOT a readable invoice. Almost certainly a login / expired link.
    if "html" in content_type:
        return _error_result(
            "url", url, "html_response_possible_login_or_expired_url",
            size_bytes=size_bytes, sha256=sha256,
        )

    # Only text/plain, application/json, and other text/* bodies are trusted as readable.
    if ("text/plain" in content_type) or ("json" in content_type) or content_type.startswith("text/"):
        body = raw.decode("utf-8", errors="replace")
        # P4 (ported) — even a 200 text/* body can be a login wall; scan and reject if so.
        if _looks_like_login(body):
            return _error_result(
                "url", url, "login_page_detected", size_bytes=size_bytes, sha256=sha256
            )
        logger.info(
            "invoice read: source=url read_path=text pages_read=None path_or_url=%s", url
        )
        return {
            "kind": "text", "source": "url", "path_or_url": url,
            "text": body, "pdf_bytes": None, "pages_read": None, "page_count": None,
            "sha256": sha256, "size_bytes": size_bytes, "read_path": "text",
            "est_input_tokens": len(body) // config.CHARS_PER_TOKEN, "error": None,
        }

    # Unknown binary that is not a PDF — flag it; do not pretend it is readable.
    return _error_result(
        "url", url, f"unsupported_content_type: {content_type or 'unknown'}",
        size_bytes=size_bytes, sha256=sha256,
    )


# ---------------------------------------------------------------------------
# Local-file source (new)
# ---------------------------------------------------------------------------

def read_local_invoice(pdf_path: str | Path, pages: str | None) -> InvoiceResult:
    """Load a locally-resolved invoice PDF. `pages` is a "A-B" range string mined
    from invoice_lookup.csv, or None when no range is known. Never raises."""
    path_str = str(pdf_path)
    try:
        path = Path(pdf_path)
        if not path.is_file():
            return _error_result("local_file", path_str, "file_not_found")

        size_bytes = path.stat().st_size
        if size_bytes > config.INVOICE_MAX_BYTES:
            return _error_result(
                "local_file", path_str, f"oversize_>{config.INVOICE_MAX_BYTES}_bytes",
                size_bytes=size_bytes,
            )

        raw = path.read_bytes()
        sha256 = hashlib.sha256(raw).hexdigest()
        if raw[:5] != b"%PDF-":
            return _error_result(
                "local_file", path_str, "not_a_pdf_magic_bytes_missing",
                size_bytes=len(raw), sha256=sha256,
            )

        try:
            text, pdf_bytes, pages_read, read_path, page_count, est = _read_pdf(raw, pages)
        except Exception as e:  # noqa: BLE001 — corrupt/encrypted PDF etc.
            return _error_result(
                "local_file", path_str, f"pdf_parse_failed: {e}",
                size_bytes=len(raw), sha256=sha256,
            )

        logger.info(
            "invoice read: source=local_file read_path=%s pages_read=%s path_or_url=%s",
            read_path, pages_read, path_str,
        )
        return {
            "kind": "pdf", "source": "local_file", "path_or_url": path_str,
            "text": text, "pdf_bytes": pdf_bytes, "pages_read": pages_read,
            "page_count": page_count, "sha256": sha256, "size_bytes": len(raw),
            "read_path": read_path, "est_input_tokens": est, "error": None,
        }
    except Exception as e:  # noqa: BLE001 — a reader must never raise
        return _error_result("local_file", path_str, f"unexpected_error: {e}")


# ---------------------------------------------------------------------------
# Persistence view
# ---------------------------------------------------------------------------

def was_truncated(res: dict | None) -> bool:
    """True when the pages actually read (pages_read) skip a middle section —
    i.e. the config.PAGE_FULL_READ_MAX-exceeded case where only the first/last
    config.PAGE_EDGE_COUNT pages of the window were read and a deciding
    date/deal name in the middle was never seen.

    Detected as a GAP in pages_read itself: parse every comma-separated
    "A-B" part into a set of page numbers, then compare the span it would
    cover if contiguous (max - min + 1) against how many pages are actually
    in the set. A gap means some page in between was skipped.

    False for None/empty, a fully-read window (single contiguous range, no
    gap), or a fully-read CSV sub-range (e.g. "3-8" of a 50-page file) — this
    also FIXES the previous bug, which flagged any pages_read short of
    page_count as truncated even when the CSV-hinted sub-range itself was
    read in full. Accepts a bare dict so both the full InvoiceResult (on a
    WorkItem) and the persisted InvoiceSummary can be passed."""
    if not res:
        return False
    pages_read = res.get("pages_read")
    if not pages_read:
        return False
    pages: set[int] = set()
    try:
        for part in str(pages_read).split(","):
            a_str, b_str = part.split("-", 1)
            pages.update(range(int(a_str.strip()), int(b_str.strip()) + 1))
    except (ValueError, AttributeError):
        return False
    if not pages:
        return False
    return (max(pages) - min(pages) + 1) > len(pages)


def to_summary(res: InvoiceResult) -> InvoiceSummary:
    """Strip text/pdf_bytes for persistence — the view stored on a DecisionRecord.
    Caller decides whether to store this or None (invoice is only
    present on the record when a document was actually read)."""
    summary: InvoiceSummary = {
        "pages_read": res.get("pages_read"),
        "page_count": res.get("page_count"),
        "sha256": res.get("sha256"),
        "size_bytes": res.get("size_bytes"),
        "read_path": res.get("read_path"),
        "error": res.get("error"),
    }
    kind = res.get("kind")
    if kind is not None:
        summary["kind"] = kind
    source = res.get("source")
    if source is not None:
        summary["source"] = source
    path_or_url = res.get("path_or_url")
    if path_or_url is not None:
        summary["path_or_url"] = path_or_url
    return summary
