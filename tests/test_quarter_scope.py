"""tests/test_quarter_scope.py — the guided-run quarter -> scope derivation
(Phase 3 Part A: `run --quarter` / `run --guided`) and ingest.filter_scope's
`months` parsing.

Pins:
  1. deal_profile.months_of_quarter: pure "2026Q1" -> ["202601", "202602",
     "202603"] arithmetic, the inverse of quarter_of at month granularity.
  2. cli._quarter_scope: the derived (quarters_arg, months_arg) pair --
     months_arg MUST carry a trailing comma. ingest.filter_scope only takes
     the explicit-period-list branch when "," is present in the months
     string (checked before the all-digit-count branch); without the
     trailing comma, a bare "202601,202602,202603" would still hit the comma
     branch. (Historically a single-month quarter's bare months_arg
     ("202601") would otherwise have been misread as "latest 202601
     PERIODS" -- i.e. the whole file -- instead of one explicit period; the
     bare-YYYYMM disambiguation rule below now also catches that case, but
     cli._quarter_scope still emits the trailing comma unconditionally, so
     this stays pinned regardless.)
  3. ingest.filter_scope's `months` disambiguation: a bare (comma-free)
     6-digit token is a literal single YYYYMM period when its last two
     digits are a valid month (01-12), checked BEFORE the plain-count
     branch, since a 6-digit count would otherwise also satisfy `.isdigit()`.
"""

from __future__ import annotations

import pytest

from gna_pipeline import cli, deal_profile, ingest


# ---------------------------------------------------------------------------
# deal_profile.months_of_quarter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label, expected",
    [
        ("2026Q1", ["202601", "202602", "202603"]),
        ("2026Q2", ["202604", "202605", "202606"]),
        ("2026Q3", ["202607", "202608", "202609"]),
        ("2026Q4", ["202610", "202611", "202612"]),
        ("2025q4", ["202510", "202511", "202512"]),  # case-insensitive
    ],
)
def test_months_of_quarter(label, expected):
    assert deal_profile.months_of_quarter(label) == expected


@pytest.mark.parametrize("bad", ["", "2026", "Q1", "2026Q5", "2026-Q1", "20260Q1", "notaquarter"])
def test_months_of_quarter_rejects_malformed_label(bad):
    with pytest.raises(ValueError):
        deal_profile.months_of_quarter(bad)


# ---------------------------------------------------------------------------
# cli._quarter_scope — the (quarters_arg, months_arg) derivation shared by
# both --quarter and --guided.
# ---------------------------------------------------------------------------

def test_quarter_scope_derives_trailing_comma_months():
    quarters_arg, months_arg = cli._quarter_scope("2026Q1")
    assert quarters_arg == "2026Q1"
    assert months_arg == "202601,202602,202603,"
    assert months_arg.endswith(",")  # mandatory -- see module docstring


def test_quarter_scope_rejects_malformed_label():
    with pytest.raises(ValueError):
        cli._quarter_scope("not-a-quarter")


# ---------------------------------------------------------------------------
# ingest.filter_scope — `months` disambiguation (Stream D)
# ---------------------------------------------------------------------------

def _scope_packet(period: str, amount: float = 100.0) -> dict:
    """Minimal RowPacket-shaped dict; filter_scope only reads period/amount/
    amount_was_blank."""
    return {"period": period, "amount": amount, "amount_was_blank": False}


_MULTI_PERIOD_PACKETS = [
    _scope_packet("202605"),
    _scope_packet("202606"),
    _scope_packet("202607"),
    _scope_packet("202608"),
]


def test_bare_yyyymm_selects_only_that_one_period_not_the_whole_file():
    kept, stats = ingest.filter_scope(_MULTI_PERIOD_PACKETS, months="202607", min_usd=0)
    assert [p["period"] for p in kept] == ["202607"]
    assert stats["periods_all"] == ["202605", "202606", "202607", "202608"]


def test_bare_yyyymm_absent_period_raises_value_error():
    # "202612" has a valid month (12) so it takes the literal-period branch,
    # but no such period exists in the fabricated dataset (202605-202608).
    with pytest.raises(ValueError):
        ingest.filter_scope(_MULTI_PERIOD_PACKETS, months="202612", min_usd=0)


def test_short_digit_string_still_means_a_count_not_a_period():
    """"6" is only 1 digit -- it never matches the 6-digit YYYYMM shape, so
    it still means "the latest 6 periods" (here, all 4 -- there are fewer
    than 6 available)."""
    kept, _stats = ingest.filter_scope(_MULTI_PERIOD_PACKETS, months="6", min_usd=0)
    assert sorted({p["period"] for p in kept}) == ["202605", "202606", "202607", "202608"]

    # A count smaller than the number of periods present actually narrows it.
    kept2, _stats2 = ingest.filter_scope(_MULTI_PERIOD_PACKETS, months="2", min_usd=0)
    assert sorted({p["period"] for p in kept2}) == ["202607", "202608"]


def test_six_digit_string_with_invalid_month_falls_through_to_count_branch():
    """"100000" is 6 digits but month "00" is not 1-12, so it does NOT match
    the literal-period rule -- it falls through to the plain-count branch
    (a huge count -> keeps everything, same as "all" here)."""
    kept, _stats = ingest.filter_scope(_MULTI_PERIOD_PACKETS, months="100000", min_usd=0)
    assert sorted({p["period"] for p in kept}) == ["202605", "202606", "202607", "202608"]
