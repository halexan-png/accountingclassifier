"""tests/test_idle_shutdown.py — idle auto-shutdown policy (gna_server.lifecycle).

The watchdog thread and uvicorn wiring aren't exercised here (that needs a live
server); instead this pins the two pure, side-effect-free pieces the whole
feature hinges on -- the shutdown decision and the timeout resolution -- plus
touch()/seconds_since_activity() bookkeeping. These are the parts a future edit
is most likely to get subtly wrong (e.g. shutting down mid-run, or a bad env
value silently disabling auto-shutdown instead of falling back to the default).
"""

from __future__ import annotations

import time

import pytest

from gna_server import lifecycle


# --- should_shut_down: the core policy -------------------------------------

def test_shuts_down_when_idle_past_timeout_and_no_run():
    assert lifecycle.should_shut_down(timeout_s=300.0, idle_s=301.0, run_active=False) is True


def test_shuts_down_exactly_at_the_deadline():
    # idle == timeout is "reached", not "not yet" -- boundary is inclusive.
    assert lifecycle.should_shut_down(timeout_s=300.0, idle_s=300.0, run_active=False) is True


def test_does_not_shut_down_before_timeout():
    assert lifecycle.should_shut_down(timeout_s=300.0, idle_s=299.0, run_active=False) is False


def test_never_shuts_down_during_an_active_run():
    # Even if idle far exceeds the timeout: a long classification run makes no
    # other HTTP calls, so the ONLY thing keeping the server alive is this gate.
    assert lifecycle.should_shut_down(timeout_s=300.0, idle_s=99999.0, run_active=True) is False


def test_disabled_timeout_never_shuts_down():
    assert lifecycle.should_shut_down(timeout_s=0.0, idle_s=99999.0, run_active=False) is False


# --- resolve_timeout_seconds: env parsing ----------------------------------

def test_default_timeout_when_unset(monkeypatch):
    monkeypatch.delenv("GNA_UI_IDLE_TIMEOUT_MIN", raising=False)
    assert lifecycle.resolve_timeout_seconds() == pytest.approx(15 * 60.0)


def test_blank_env_uses_default(monkeypatch):
    monkeypatch.setenv("GNA_UI_IDLE_TIMEOUT_MIN", "   ")
    assert lifecycle.resolve_timeout_seconds() == pytest.approx(15 * 60.0)


def test_env_override_sets_minutes(monkeypatch):
    monkeypatch.setenv("GNA_UI_IDLE_TIMEOUT_MIN", "2")
    assert lifecycle.resolve_timeout_seconds() == pytest.approx(120.0)


def test_zero_disables(monkeypatch):
    monkeypatch.setenv("GNA_UI_IDLE_TIMEOUT_MIN", "0")
    assert lifecycle.resolve_timeout_seconds() == 0.0


def test_negative_disables(monkeypatch):
    monkeypatch.setenv("GNA_UI_IDLE_TIMEOUT_MIN", "-3")
    assert lifecycle.resolve_timeout_seconds() == 0.0


def test_garbage_falls_back_to_default_not_disabled(monkeypatch):
    # A typo must NOT silently turn auto-shutdown off -- fall back to the default.
    monkeypatch.setenv("GNA_UI_IDLE_TIMEOUT_MIN", "abc")
    assert lifecycle.resolve_timeout_seconds() == pytest.approx(15 * 60.0)


# --- touch / seconds_since_activity ----------------------------------------

def test_touch_resets_idle_clock():
    # now is measured relative to lifecycle's monotonic clock; touch() stamps it
    # to "roughly now", so immediately after, elapsed is ~0.
    lifecycle.touch()
    assert lifecycle.seconds_since_activity() < 1.0


def test_seconds_since_activity_grows_with_supplied_now():
    lifecycle.touch()
    before = lifecycle.seconds_since_activity()
    later = lifecycle.seconds_since_activity(now=time.monotonic() + 50.0)
    assert later >= before + 49.0


# --- start_watchdog: disabled path -----------------------------------------

def test_start_watchdog_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv("GNA_UI_IDLE_TIMEOUT_MIN", "0")
    thread = lifecycle.start_watchdog(
        request_exit=lambda: None,
        is_run_active=lambda: False,
    )
    assert thread is None
