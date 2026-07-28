"""tests/test_invoice_local_resolve.py — unit tests for the additive
filename-fallback resolution path in invoice_mining.resolve_local: an
operator can drop a folder of invoice PDFs named by invoice number — with or
without an INV-prefix/punctuation — and have them resolve with NO
invoice_lookup.csv present, as long as the filename normalizes to the same
key a row mines. See invoice_mining._resolve_via_filename_fallback /
_normalized_dir_index.

These tests exercise only the new fallback branch and its non-interference
with the pre-existing exact/prefix-glob/entityid-tie-break path, which is
untouched (see test_invariants.py + token_extraction_cases.txt for that
path's own characterization coverage). `_normalized_dir_index.cache_clear()`
is called at the top of every test: the helper is `lru_cache`d process-wide,
and asserting on its `cache_info()` is how the CONTROL tests below prove the
fallback did or did not actually run.
"""

from __future__ import annotations

from gna_pipeline import invoice_mining


def test_bare_numeric_filename_resolves_via_primary_path(tmp_path):
    """A file literally named "{key}.pdf" resolves via the pre-existing exact
    match — the fallback isn't even needed here, but this is the operator's
    simplest use case (drop a folder of invoice-number-named PDFs) and must
    keep working with no CSV present."""
    invoice_mining._normalized_dir_index.cache_clear()
    (tmp_path / "1231239.pdf").touch()

    path, pages, status = invoice_mining.resolve_local(
        "1231239", False, None, tmp_path, {}
    )

    assert status == "resolved"
    assert path == tmp_path / "1231239.pdf"
    assert pages is None
    # CONTROL: proves this resolved via the primary exact-match path, not
    # the new fallback — the fallback helper was never even called.
    info = invoice_mining._normalized_dir_index.cache_info()
    assert (info.hits, info.misses) == (0, 0)


def test_inv_prefixed_filename_resolves_via_fallback(tmp_path):
    """"INV-12345.pdf" does not start with the mined key "12345", so the
    primary exact/prefix-glob resolution finds nothing; the filename
    fallback normalizes the stem and matches it."""
    invoice_mining._normalized_dir_index.cache_clear()
    (tmp_path / "INV-12345.pdf").touch()

    path, pages, status = invoice_mining.resolve_local(
        "12345", False, None, tmp_path, {}
    )

    assert status == "resolved"
    assert path == tmp_path / "INV-12345.pdf"
    assert pages is None
    info = invoice_mining._normalized_dir_index.cache_info()
    assert info.misses == 1  # the fallback dir index was actually built


def test_fallback_collision_reports_ambiguous(tmp_path):
    """Two filenames that normalize to the same key, neither resolvable via
    the primary path, must report "ambiguous" — never guess."""
    invoice_mining._normalized_dir_index.cache_clear()
    (tmp_path / "INV-12345.pdf").touch()
    (tmp_path / "12-345.pdf").touch()

    path, pages, status = invoice_mining.resolve_local(
        "12345", False, None, tmp_path, {}
    )

    assert status == "ambiguous"
    assert path is None
    assert pages is None


def test_fallback_no_hit_stays_no_match(tmp_path):
    """An unrelated filename leaves the row "no_match", exactly as before
    the fallback existed."""
    invoice_mining._normalized_dir_index.cache_clear()
    (tmp_path / "UNRELATED-999.pdf").touch()

    path, pages, status = invoice_mining.resolve_local(
        "12345", False, None, tmp_path, {}
    )

    assert status == "no_match"
    assert path is None
    assert pages is None


def test_fallback_does_not_fire_when_primary_resolves_with_populated_index(tmp_path):
    """CONTROL: when the existing splitter-named-file convention already
    resolves a row via the prefix glob, with a populated CSV-style index
    present, the new fallback must never even run, and the CSV-backed
    `pages` value must still flow through unchanged."""
    invoice_mining._normalized_dir_index.cache_clear()
    (tmp_path / "12345__GNL1.pdf").touch()

    index = {
        "12345": [
            {
                "vendor_invoice_no": "12345",
                "invoice_key": "12345",
                "gnl_ref": "GNL1",
                "pdf_file": "original.pdf",
                "pages": "1-3",
                "gl_refs": "",
                "gl_entityids": "",
                "found_in_gl": "",
            }
        ]
    }

    path, pages, status = invoice_mining.resolve_local(
        "12345", False, None, tmp_path, index
    )

    assert status == "resolved"
    assert path == tmp_path / "12345__GNL1.pdf"
    assert pages == "1-3"  # came from the CSV-backed index, not the fallback
    info = invoice_mining._normalized_dir_index.cache_info()
    assert (info.hits, info.misses) == (0, 0)
