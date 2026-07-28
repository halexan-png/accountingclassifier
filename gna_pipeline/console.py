"""console.py — single-writer, thread-safe presentation layer for the CLI.

Every operator-facing line in the pipeline (section headers, phase stats,
per-row decisions, the live progress status, and routed `logging` records)
goes through this module so the terminal reads as one voice instead of two
clashing ones (`logging`'s "HH:MM:SS INFO gna.x: ..." vs ad-hoc
"[HH:MM:SS] ..." prints).

Everything here is presentation only — no business logic, no mutation of
anything the pipeline persists. All public functions are safe to call from
any thread; a single module-level lock (`_LOCK`) serializes actual writes to
stdout so concurrent classify/sweep worker threads never interleave mid-line.

ASCII-only: when stdout is redirected on Windows the encoding can be cp1252,
so no box-drawing characters or unicode bullets anywhere in this module.
Every write flushes explicitly (`run_gna.ps1` notes stdout is frequently not
a tty even in interactive sessions, so buffering would otherwise silently
hold lines back).

The live status line (`status()` / `clear_status()`) uses only "\r" +
padding — no ANSI escape codes — so it degrades safely on a classic
conhost console with no VT processing.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
import time
from typing import Callable

from gna_pipeline import config

_LOCK = threading.Lock()

# The line currently "owned" by the live status display (empty when there is
# none) and how many characters of the terminal line it currently occupies
# (so the next clear/redraw knows how much whitespace to overwrite).
_status_text: str = ""
_status_visible_len: int = 0

# Optional event sink: a passive observer (the UI server's run manager) that
# every writer below mirrors its payload to, so a live view can be rebuilt
# without parsing formatted console text. None (the default) means no one is
# listening -- CLI-only use is unaffected. Installed/removed by the run
# manager around a run (see set_event_sink); a sink exception is swallowed,
# never allowed to break a run.
_event_sink: "Callable[[str, dict], None] | None" = None

# Optional pluggable confirm handler -- see confirm()'s docstring.
_confirm_handler: "Callable[[str], bool] | None" = None


def set_event_sink(fn: "Callable[[str, dict], None] | None") -> None:
    """Install (fn) or remove (None) the module-level event sink. Every
    writer below (section/banner/info/warn/kv/row/status/clear_status) calls
    it as `fn(event_type, payload)` while `_LOCK` is held; a sink exception
    is swallowed (see `_sink_emit`) so a bug in the UI's sink can never break
    a run."""
    global _event_sink
    with _LOCK:
        _event_sink = fn


def _sink_emit(kind: str, payload: dict) -> None:
    """MUST be called with `_LOCK` already held. Never raises."""
    if _event_sink is None:
        return
    try:
        _event_sink(kind, payload)
    except Exception:  # noqa: BLE001 — a sink bug must never break a run
        pass


def set_confirm_handler(fn: "Callable[[str], bool] | None") -> None:
    """Install (fn) or remove (None) the pluggable confirm handler used by
    `confirm()` below in place of the terminal `input()` prompt."""
    global _confirm_handler
    with _LOCK:
        _confirm_handler = fn


def _detect_tty() -> bool:
    override = os.environ.get("GNA_FORCE_TTY")
    if override is not None:
        return override.strip() not in ("", "0", "false", "False")
    try:
        return sys.stdout.isatty()
    except Exception:  # noqa: BLE001 — stdout can be an odd wrapper; fail to non-tty
        return False


_IS_TTY = _detect_tty()


def force_tty(value: bool | None) -> None:
    """Override tty detection (tests / demo scripts). None re-runs auto-detect."""
    global _IS_TTY
    with _LOCK:
        _IS_TTY = _detect_tty() if value is None else bool(value)


def is_tty() -> bool:
    return _IS_TTY


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _term_width() -> int:
    try:
        return shutil.get_terminal_size().columns
    except Exception:  # noqa: BLE001
        return 200


def _emit_locked(line: str) -> None:
    """Write one payload line. MUST be called with `_LOCK` already held.

    Clears whatever status text is currently visible, writes `line` + a
    newline, then redraws the status text fresh below it (if any) so the
    live line survives ordinary output without tearing.
    """
    global _status_visible_len
    if _IS_TTY and _status_visible_len:
        sys.stdout.write("\r" + " " * _status_visible_len + "\r")
    sys.stdout.write(line + "\n")
    if _IS_TTY and _status_text:
        sys.stdout.write("\r" + _status_text)
        _status_visible_len = len(_status_text)
    else:
        _status_visible_len = 0
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Public writers
# ---------------------------------------------------------------------------

def section(title: str) -> None:
    """Blank line, then a `==== <title> ` header padded with '=' to ~72 cols."""
    header = f"==== {title} "
    if len(header) < 72:
        header = header + "=" * (72 - len(header))
    with _LOCK:
        _emit_locked("\n" + header)
        _sink_emit("section", {"title": title})


def banner(lines: "str | list[str]") -> None:
    """A boxed, hard-to-miss announcement -- for a heads-up an operator must
    not scroll past (e.g. invoice-lookup availability ahead of a paid
    classify phase), one notch louder than `section()`. ASCII-only, same
    '=' rule style, just full-width and multi-line-capable."""
    if isinstance(lines, str):
        lines = [lines]
    rule = "=" * 72
    with _LOCK:
        _emit_locked("\n" + rule)
        for line in lines:
            _emit_locked(f"  {line}")
        _emit_locked(rule)
        _sink_emit("banner", {"lines": list(lines)})


def info(msg: str) -> None:
    with _LOCK:
        _emit_locked(f"[{_ts()}] {msg}")
        _sink_emit("info", {"msg": msg})


def warn(msg: str) -> None:
    with _LOCK:
        _emit_locked(f"[{_ts()}] !! {msg}")
        _sink_emit("warn", {"msg": msg})


def kv(pairs: "dict[str, str] | list[tuple[str, str]]", *, indent: int = 2) -> None:
    """Aligned `key: value` lines (stats / forecast blocks)."""
    items = list(pairs.items()) if isinstance(pairs, dict) else list(pairs)
    if not items:
        return
    key_width = max(len(str(k)) for k, _ in items)
    lines = [f"{' ' * indent}{str(k).ljust(key_width)}: {v}" for k, v in items]
    with _LOCK:
        for line in lines:
            _emit_locked(line)
        _sink_emit("kv", {"pairs": [[str(k), str(v)] for k, v in items]})


def data(kind: str, payload: dict) -> None:
    """Structured data channel: prints nothing, sink-only. Called from the
    existing print helpers (print_phase0_stats, print_phase1_forecast,
    _print_phase2_forecast, print_context_report, print_closing_tally) with
    their raw dicts, plus one data("outputs", {...}) at the end of the run."""
    with _LOCK:
        _sink_emit("data", {"kind": kind, "payload": payload})


def _fmt_amount(amount: float, currency: str) -> str:
    sign = "-" if amount < 0 else ""
    text = f"{sign}${abs(amount):,.2f}"
    if currency and currency not in ("USD", "UNKNOWN"):
        text = f"{text} {currency}"
    return text


def _truncate(text: str, width: int) -> str:
    text = text or ""
    return text if len(text) <= width else text[: max(0, width - 3)] + "..."


def row(record: dict, phase: str) -> None:
    """One terse, fixed-width per-row decision line. Safe to call from
    worker threads — serialized via `_LOCK` like everything else here.

    `phase` is "classify" or "sweep" (or "phase0"); it only changes which
    optional suffixes get attached (a sweep row has no meaningful
    basis distinction beyond the M&A rule, but DOES carry a
    sweep ok/failed outcome that the old per-row print used to show).
    """
    packet = record.get("packet") or {}
    decision = record.get("decision") or {}
    flags = record.get("flags") or []
    is_error = bool(record.get("error"))

    row_idx = record.get("row_idx", packet.get("row_idx", 0))
    acctnum = packet.get("acctnum") or "-"
    amount = packet.get("amount") or 0.0
    currency = packet.get("currency") or "USD"
    classification = decision.get("classification") or ("error" if is_error else "-")
    basis = decision.get("basis") or "-"
    recognized_deal = decision.get("recognized_deal") or "none"
    missing_info = decision.get("missing_info")
    error = record.get("error")

    exception_row = is_error or classification in ("non_recurring", "human_review")
    marker = "!" if exception_row else " "

    line = (
        f"{marker}row {row_idx:>6} {str(acctnum):<12} {_fmt_amount(amount, currency):>14} "
        f"{classification:<14} {basis}"
    )

    if phase == "sweep" and "deal_sweep_failed" in flags:
        line += "  !! sweep_failed"
    elif recognized_deal not in ("", "none"):
        line += f"  deal:{recognized_deal}"

    if is_error:
        line += f"  !! error: {_truncate(str(record.get('error') or ''), 60)}"
    elif classification == "human_review":
        missing = decision.get("missing_info") or ""
        line += f"  ! {_truncate(missing, 50)}"

    if _IS_TTY:
        width = _term_width()
        if len(line) > width:
            line = line[:width]

    with _LOCK:
        _emit_locked(line)
        _sink_emit("row", {
            "row_idx": row_idx,
            "acctnum": acctnum,
            "amount": amount,
            "currency": currency,
            "classification": classification,
            "basis": basis,
            "recognized_deal": recognized_deal,
            "flags": list(flags),
            "phase": phase,
            "missing_info": missing_info,
            "error": error,
        })


# ---------------------------------------------------------------------------
# Live status line
# ---------------------------------------------------------------------------

def status(text: str, *, force: bool = False, snapshot: "dict | None" = None) -> None:
    """Update the live in-place status line.

    TTY: overwrite the current line in place (no scrollback growth).
    Non-tty: prints nothing unless `force=True` (used once per completed
    batch), in which case it prints as a plain, durable `[HH:MM:SS] <text>`
    line — there is no "in place" on a redirected stream.

    `snapshot` (optional) is `Progress.snapshot()`'s dict — passed straight
    through to the sink alongside `text` so a UI can render rows/batches/
    cost/ETA numbers without parsing this line's formatted text.
    """
    global _status_text, _status_visible_len
    with _LOCK:
        _status_text = text
        if _IS_TTY:
            width = _term_width()
            visible = text[:width] if len(text) > width else text
            padded = visible.ljust(min(_status_visible_len, width))
            sys.stdout.write("\r" + padded)
            sys.stdout.flush()
            _status_visible_len = len(visible)
        elif force:
            sys.stdout.write(f"[{_ts()}] {text}\n")
            sys.stdout.flush()
        _sink_emit("status", {"text": text, "snapshot": snapshot})


def clear_status() -> None:
    """Erase the live status line (if any) and forget it — call this before
    any `input()` prompt (which bypasses this module entirely) and at the
    end of a phase/run so nothing stale lingers on the line."""
    global _status_text, _status_visible_len
    with _LOCK:
        if _IS_TTY and _status_visible_len:
            sys.stdout.write("\r" + " " * _status_visible_len + "\r")
            sys.stdout.flush()
        _status_text = ""
        _status_visible_len = 0
        _sink_emit("status", {"text": "", "snapshot": None})


def confirm(prompt: str) -> bool:
    """One shared y/N confirmation gate: clear the live status line (so the
    prompt never lands mid-redraw), then defer to the pluggable confirm
    handler if one is installed (set_confirm_handler — the UI server's money
    gate bridge), else a plain `input()` -- this bypasses the module's own
    writer exactly like every existing confirm gate in cli.py/pipeline.py
    already does; this just gives that pattern one name instead of
    re-inlining `clear_status()` + `input()` at each call site."""
    clear_status()
    handler = _confirm_handler
    if handler is not None:
        return bool(handler(prompt))
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        # stdin isn't interactive (no tty attached, redirected from
        # /dev/null, etc.) -- never auto-assume "yes" on a money gate;
        # decline safely instead of letting the EOFError traceback crash
        # the run.
        warn("no confirmation possible: stdin is not interactive -- declining")
        return False
    return answer in ("y", "yes")


# ---------------------------------------------------------------------------
# Progress — shared by classify.run_classification (Phase 2) and
# deal_profile.run_sweep (Phase 1).
# ---------------------------------------------------------------------------

def _format_eta(seconds: float) -> str:
    minutes = seconds / 60.0
    if minutes < 1:
        return "<1m"
    return f"~{round(minutes)}m"


class Progress:
    """Tracks one phase's live progress and renders the status line text.

    Two ways to record a completed batch (both call sites are on the MAIN
    thread, per `run_batches`' `on_complete` contract):
      - `batch_done(records=[...])` — classify.py: sums row count + usage
        straight from the batch's DecisionRecords (only the first record of
        a batch carries real usage; summing every record's usage never
        double-counts since the rest are zero), and prices THIS batch off
        `records[0]["model_version"]` (a batch can run on the floor or on
        config.INVOICE_MODEL — see config.model_for_batch), accumulating
        into `self._cost`.
      - `batch_done(done_rows=N, usage={...}, cost=C)` — deal_profile.py: its
        on_complete already holds the authoritative shared accumulators
        (stats/usage_total/cost_total), so it passes absolute snapshots
        instead (usage and cost are SET, not added, in this branch).

    `self._cost` (not a single `self.model` pricing rate applied to
    `self.usage`) is the one source of truth `render()`/`snapshot()` show —
    a run mixing two models has no single correct blended rate. `self.model`
    is kept only for the header label (e.g. "sonnet-4-6 + sonnet-5" is a
    fine value to pass there); it prices nothing.

    `batch_started()` is called from WORKER threads (as a batch begins
    processing) to bump `in_flight`; it and `batch_done` share `_lock` (a
    lock private to this Progress instance, not the console module lock).
    """

    def __init__(self, *, total_rows: int, total_batches: int, model: str | None, unit: str = "rows") -> None:
        self.total_rows = total_rows
        self.total_batches = total_batches
        self.unit = unit
        self.model = model
        self.done_rows = 0
        self.done_batches = 0
        self.in_flight = 0
        self.usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        self._cost = 0.0
        self._start = time.monotonic()
        self._lock = threading.Lock()

    def batch_started(self) -> None:
        with self._lock:
            self.in_flight += 1

    def batch_done(
        self,
        records: "list[dict] | None" = None,
        *,
        done_rows: int | None = None,
        usage: "dict | None" = None,
        cost: float | None = None,
    ) -> None:
        with self._lock:
            self.in_flight = max(0, self.in_flight - 1)
            self.done_batches += 1
            if records is not None:
                self.done_rows += len(records)
                for r in records:
                    u = r.get("usage") or {}
                    for k in self.usage:
                        self.usage[k] += u.get(k, 0)
                if records:
                    batch_model = records[0].get("model_version") or self.model or config.DEFAULT_MODEL
                    batch_usage = records[0].get("usage") or {}
                    pricing = config.pricing_for(batch_model)
                    self._cost += (
                        batch_usage.get("input_tokens", 0) / 1_000_000 * pricing["input"]
                        + batch_usage.get("output_tokens", 0) / 1_000_000 * pricing["output"]
                    )
            else:
                if done_rows is not None:
                    self.done_rows = done_rows
                if usage is not None:
                    for k in self.usage:
                        self.usage[k] = usage.get(k, 0)
                if cost is not None:
                    self._cost = cost

    def snapshot(self) -> dict:
        """Point-in-time progress dict for the UI's live view (§7.2 `status`
        event's `snapshot` field) — the same numbers `render()` formats into
        text, as plain values instead."""
        with self._lock:
            done_rows, total_rows = self.done_rows, self.total_rows
            done_batches, total_batches = self.done_batches, self.total_batches
            in_flight = self.in_flight
            cost = self._cost

        if done_batches > 0 and done_rows > 0:
            elapsed = time.monotonic() - self._start
            remaining = max(0, total_rows - done_rows)
            eta_s: float | None = (elapsed / done_rows) * remaining
        else:
            eta_s = None

        return {
            "done_rows": done_rows,
            "total_rows": total_rows,
            "done_batches": done_batches,
            "total_batches": total_batches,
            "in_flight": in_flight,
            "cost_usd": cost,
            "eta_s": eta_s,
            "unit": self.unit,
        }

    def render(self) -> str:
        snap = self.snapshot()
        eta = "--" if snap["eta_s"] is None else _format_eta(snap["eta_s"])
        return (
            f"{snap['done_rows']}/{snap['total_rows']} {snap['unit']} | "
            f"{snap['done_batches']}/{snap['total_batches']} batches | "
            f"{snap['in_flight']} in flight | ${snap['cost_usd']:.2f} | ETA {eta}"
        )


# ---------------------------------------------------------------------------
# logging integration
# ---------------------------------------------------------------------------

class ConsoleLogHandler(logging.Handler):
    """Routes every logging record through this module's writer so `logging`
    output shares the same voice, status-line clear/redraw, and ASCII-only
    guarantee as everything else. Format: `[HH:MM:SS] LEVEL name: message`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            ts = time.strftime("%H:%M:%S", time.localtime(record.created))
            line = f"[{ts}] {record.levelname} {record.name}: {msg}"
            with _LOCK:
                _emit_locked(line)
        except Exception:  # noqa: BLE001 — a logging handler must never raise
            self.handleError(record)
