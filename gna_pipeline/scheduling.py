"""scheduling.py — the batching + rate-limiting + concurrent-pool scheduler
shared by Phase 1 (deal_profile.run_sweep) and Phase 2 (classify.run_classification).

One scheduler, not two: both phases size their batches the same way, gate
requests through the same rolling-60s rate limiter, derive worker count from
the same measured-limits formula, and run through the same warmup +
bounded-pool + Ctrl-C-safe driver (`run_batches`).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from gna_pipeline import config, console
from gna_pipeline.contract import WorkItem


class SpendCapExceeded(Exception):
    """Runtime spend rail: raised from a phase's on_complete (main thread)
    when actual accumulated cost crosses config.SPEND_CAP_MULTIPLIER x that
    phase's own forecast high end. `run_batches` treats it exactly like
    KeyboardInterrupt — cancel queued batches, let in-flight finish, re-raise —
    so everything already decided is salvaged through the same proven path."""


class OperatorCancelled(KeyboardInterrupt):
    """Cooperative-cancel exception for the UI's Stop button.

    Subclasses KeyboardInterrupt (NOT a bare Exception) so it flows through
    every existing `except (KeyboardInterrupt, SpendCapExceeded)` catch site
    (run_batches below; pipeline.py's Phase-1/Phase-2 stages; cli.py's
    standalone deal-profile command) completely unmodified — no
    except-tuple widening anywhere, no risk of missing a site. Salvage
    semantics are exactly the Ctrl-C path: whatever was already durable in
    results.jsonl before the cancel stays durable.
    """


# Module-level cooperative-cancel event, installed by the UI run manager
# around a run (set_cancel_event) and checked at the three points named in
# OperatorCancelled's docstring's callers: run_batches' as_completed loop
# (before each on_complete), RateLimiter.acquire's poll loop (so a worker
# already parked there doesn't fire its paid request after Stop), and
# immediately before each batch is submitted. None (the default) means
# nothing is watching -- CLI-only use never checks it, zero behavior change.
_cancel_event: "threading.Event | None" = None


def set_cancel_event(ev: "threading.Event | None") -> None:
    """Install (ev) or remove (None) the module-level cooperative-cancel
    event."""
    global _cancel_event
    _cancel_event = ev


def _cancel_requested() -> bool:
    return _cancel_event is not None and _cancel_event.is_set()


# ---------------------------------------------------------------------------
# Dynamic token-budget batching
# ---------------------------------------------------------------------------

def size_batches(items: list[WorkItem], target_tokens: int) -> list[list[WorkItem]]:
    """Greedy-fill batches to `target_tokens` of estimated input cost.

    Rows are sorted by (acctnum, category, descrptn) purely so a human reading
    the JSONL sees coherent groups — it does not affect any row's independent
    decision. Each row is charged `item["est_input_tokens"]` (already includes
    any invoice Phase 0 resolved). A single item whose own cost exceeds
    `target_tokens` gets its own batch rather than being split against itself.
    A vision-read item always gets its own batch too, regardless of size, so a
    slow/stuck PDF-vision request only ever affects its own row, never
    innocent text rows sharing a batch.
    """
    ordered = sorted(
        items,
        key=lambda it: (
            it["packet"]["acctnum"],
            it["packet"].get("category") or "",
            it["packet"].get("descrptn") or "",
        ),
    )

    batches: list[list[WorkItem]] = []
    current: list[WorkItem] = []
    current_tokens = 0

    for item in ordered:
        cost = item.get("est_input_tokens", 0)
        is_vision = (item.get("invoice") or {}).get("read_path") == "vision"
        if is_vision or cost > target_tokens:
            if current:
                batches.append(current)
                current, current_tokens = [], 0
            batches.append([item])
            continue
        if current and current_tokens + cost > target_tokens:
            batches.append(current)
            current, current_tokens = [], 0
        current.append(item)
        current_tokens += cost

    if current:
        batches.append(current)

    return batches


def _parse_pages_read(pages_read: Any) -> int | None:
    """"A-B" or "A-B,C-D,..." -> total page count summed across
    comma-separated ranges (e.g. "1-2,24-25" -> 4), or None if
    unparsable/absent."""
    if not isinstance(pages_read, str) or not pages_read:
        return None
    total = 0
    try:
        for part in pages_read.split(","):
            a_str, b_str = part.split("-", 1)
            n = int(b_str.strip()) - int(a_str.strip()) + 1
            if n <= 0:
                return None
            total += n
    except (ValueError, AttributeError):
        return None
    return total if total > 0 else None


def low_end_tokens(item: WorkItem) -> int:
    """Recompute one invoice-bearing item's input-token estimate at the LOW
    end: text rows are already exact (len(text)/4, same at both ends); vision
    rows swap the HIGH per-page rate baked into est_input_tokens for the LOW
    one. Reads the invoice dict defensively — any missing/malformed field
    falls back to 0.75 * est_input_tokens. Rows with no read invoice are
    unaffected (their high == low)."""
    est = item.get("est_input_tokens", 0) or 0
    invoice = item.get("invoice")
    if not invoice:
        return est
    kind = invoice.get("kind")
    if kind not in ("pdf", "text"):
        return est

    try:
        read_path = invoice.get("read_path")
        invoice_high = invoice.get("est_input_tokens", 0) or 0
        row_base = max(est - invoice_high, 0)

        if read_path == "text":
            text = invoice.get("text") or ""
            invoice_low = len(text) // config.CHARS_PER_TOKEN
        elif read_path == "vision":
            pages = _parse_pages_read(invoice.get("pages_read"))
            if pages is None:
                return int(0.75 * est)
            # A native PDF block bills per-page image tokens AND the extracted
            # text layer; _read_pdf's HIGH est sums both, so the LOW end swaps
            # only the per-page rate (HIGH -> LOW) and keeps the text component.
            text = invoice.get("text") or ""
            invoice_low = (
                pages * config.VISION_TOKENS_PER_PAGE[0]
                + len(text) // config.CHARS_PER_TOKEN
            )
        else:
            return int(0.75 * est)

        return row_base + invoice_low
    except Exception:  # noqa: BLE001 — forecast must never crash on odd data
        return int(0.75 * est)


# ---------------------------------------------------------------------------
# Rate limiter — RPM bucket + rolling-60s input/output-token budgets.
# ---------------------------------------------------------------------------

class RateLimiter:
    """Thread-safe gate: acquire() blocks until the rolling-60s request
    count, input-token sum, AND output-token sum all have room for the next
    call. Charges all three budgets on admission (the output-token charge is
    an estimate — the row cap MAX_TOKENS_PER_ROW_OUTPUT times batch size —
    since actual output tokens aren't known until the response lands)."""

    def __init__(self, rpm_cap: int, itpm_cap: int | None, otpm_cap: int | None) -> None:
        self._rpm_cap = rpm_cap
        self._itpm_cap = itpm_cap
        self._otpm_cap = otpm_cap
        self._lock = threading.Lock()
        self._request_times: deque[float] = deque()
        self._token_events: deque[tuple[float, int]] = deque()
        self._token_sum = 0
        self._output_events: deque[tuple[float, int]] = deque()
        self._output_sum = 0

    def _evict(self, now: float) -> None:
        cutoff = now - 60.0
        while self._request_times and self._request_times[0] < cutoff:
            self._request_times.popleft()
        while self._token_events and self._token_events[0][0] < cutoff:
            _, tok = self._token_events.popleft()
            self._token_sum -= tok
        while self._output_events and self._output_events[0][0] < cutoff:
            _, tok = self._output_events.popleft()
            self._output_sum -= tok

    def acquire(self, input_tokens: int, output_tokens: int) -> None:
        # Clamp each charge to its whole-window cap: a single request whose
        # estimate exceeds the cap could otherwise never be admitted and this
        # loop would spin forever. Clamped, it occupies the entire window
        # alone and the API itself becomes the final arbiter of "too large".
        if self._itpm_cap is not None:
            input_tokens = min(input_tokens, self._itpm_cap)
        if self._otpm_cap is not None:
            output_tokens = min(output_tokens, self._otpm_cap)
        while True:
            if _cancel_requested():
                raise OperatorCancelled("cancelled while waiting for rate-limit budget")
            with self._lock:
                now = time.monotonic()
                self._evict(now)
                requests_ok = len(self._request_times) < self._rpm_cap
                input_ok = self._itpm_cap is None or self._token_sum + input_tokens <= self._itpm_cap
                output_ok = (
                    self._otpm_cap is None or self._output_sum + output_tokens <= self._otpm_cap
                )
                if requests_ok and input_ok and output_ok:
                    self._request_times.append(now)
                    self._token_events.append((now, input_tokens))
                    self._token_sum += input_tokens
                    self._output_events.append((now, output_tokens))
                    self._output_sum += output_tokens
                    return
            time.sleep(0.25)


def build_limiter(rate_limits: dict | None) -> RateLimiter:
    requests_limit = rate_limits.get("requests_limit") if rate_limits else None
    if isinstance(requests_limit, (int, float)) and requests_limit > 0:
        rpm_cap = max(1, int(requests_limit * 0.8))
    else:
        rpm_cap = 30  # default when unmeasured

    input_tokens_limit = rate_limits.get("input_tokens_limit") if rate_limits else None
    if isinstance(input_tokens_limit, (int, float)) and input_tokens_limit > 0:
        itpm_cap: int | None = int(input_tokens_limit * 0.8)
    else:
        itpm_cap = None  # unlimited when unmeasured

    output_tokens_limit = rate_limits.get("output_tokens_limit") if rate_limits else None
    if isinstance(output_tokens_limit, (int, float)) and output_tokens_limit > 0:
        otpm_cap: int | None = int(output_tokens_limit * 0.8)
    else:
        otpm_cap = None  # unlimited when unmeasured

    return RateLimiter(rpm_cap, itpm_cap, otpm_cap)


def compute_max_workers(
    rate_limits: dict | None,
    *,
    batch_target_tokens: int,
    est_output_per_batch: int,
    latency_s: float,
    n_batches: int,
) -> int:
    """Derive how many batches can run concurrently, sustained, without
    breaching measured RPM/ITPM/OTPM. One worker sustains `60/latency_s`
    batches/min; dividing each 80%-margined budget by that rate and per-batch
    cost gives the worker count that budget alone would allow, and we take
    the tightest of the three, then clamp to [1, MAX_WORKERS_CEILING,
    n_batches].

    `rate_limits` None, or none of its three limits parsing as a positive
    number, falls back to the pre-measurement default (MAX_WORKERS_UNMEASURED).
    """
    if n_batches <= 0:
        return 1

    def _positive(x: Any) -> bool:
        return isinstance(x, (int, float)) and x > 0

    rpm = rate_limits.get("requests_limit") if rate_limits else None
    itpm = rate_limits.get("input_tokens_limit") if rate_limits else None
    otpm = rate_limits.get("output_tokens_limit") if rate_limits else None

    if not (_positive(rpm) or _positive(itpm) or _positive(otpm)):
        return max(1, min(config.MAX_WORKERS_UNMEASURED, n_batches))

    rate = 60.0 / latency_s  # batches/min one worker sustains
    candidates: list[float] = []
    if _positive(rpm):
        candidates.append(0.8 * rpm / rate)
    if _positive(itpm):
        candidates.append(0.8 * itpm / (batch_target_tokens * rate))
    if _positive(otpm):
        candidates.append(0.8 * otpm / (max(1, est_output_per_batch) * rate))

    return max(1, min(int(min(candidates)), config.MAX_WORKERS_CEILING, n_batches))


# ---------------------------------------------------------------------------
# Shared scheduler — warmup + concurrent pool + Ctrl-C-safe shutdown.
# ---------------------------------------------------------------------------

def run_batches(
    batches: list,
    process_one: Callable[[Any], Any],
    *,
    max_workers: int,
    on_complete: Callable[[Any], None],
    interrupt_label: str,
) -> None:
    """Run `process_one` over `batches[0]` alone (warmup — writes the prompt
    cache), then the rest on a ThreadPoolExecutor(max_workers), calling
    `on_complete(result)` on the MAIN thread as each future resolves
    (never inside a worker thread — callers rely on this to mutate
    non-thread-safe state from `on_complete` without their own locking).

    `process_one` must never raise — both phases' batch processors already
    guarantee a result no matter what happens (never-drop-a-row / never-
    raise-out-of-a-worker); if one somehow did, the exception would surface
    from `fut.result()` and unwind this function without running the
    remaining batches.

    On KeyboardInterrupt OR SpendCapExceeded (raised by a caller's
    on_complete when the runtime spend rail trips) OR OperatorCancelled
    (raised by a worker's RateLimiter.acquire, or by this function itself
    when the module-level cancel event trips — see set_cancel_event):
    cancel every not-yet-started future, let in-flight requests finish or
    time out (bounded by the client/per-request timeout), print, and
    re-raise — the caller decides what "interrupted" means for its own
    bookkeeping. OperatorCancelled IS a KeyboardInterrupt (see its
    docstring), so it takes this same path with no extra branch.
    """
    if not batches:
        return

    first_batch, remaining = batches[0], batches[1:]
    on_complete(process_one(first_batch))

    if not remaining:
        return

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        try:
            # Submission happens INSIDE this try (not in a bare list-comp
            # before it) so a cancel that trips mid-submission is caught by
            # the same except below, not left to unwind straight into the
            # executor's __exit__ (shutdown(wait=True)), which would let
            # every already-submitted-but-not-yet-cancelled future run
            # anyway.
            futures = []
            for b in remaining:
                if _cancel_requested():
                    raise OperatorCancelled(
                        f"{interrupt_label}: cancelled before all batches were submitted"
                    )
                futures.append(executor.submit(process_one, b))
            for fut in as_completed(futures):
                if _cancel_requested():
                    raise OperatorCancelled(
                        f"{interrupt_label}: cancelled while batches were in flight"
                    )
                on_complete(fut.result())
        except (KeyboardInterrupt, SpendCapExceeded) as exc:
            # Without this, the exception propagates straight into the
            # executor's __exit__ (shutdown(wait=True)), which lets every
            # queued-but-not-started batch run anyway and spend money.
            # Cancel what hasn't started; in-flight requests are bounded
            # by the client timeout, so __exit__'s wait=True below only
            # waits on those.
            executor.shutdown(wait=False, cancel_futures=True)
            reason = (
                "interrupted" if isinstance(exc, KeyboardInterrupt)
                else f"SPEND RAIL TRIPPED ({exc})"
            )
            console.warn(
                f"{interrupt_label}: {reason} -- cancelled queued batches; in-flight "
                f"request(s) will finish or time out; rows already decided are safe in "
                f"results.jsonl"
            )
            raise
