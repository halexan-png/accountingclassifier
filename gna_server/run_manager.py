"""run_manager.py — the server's run-manager core (v2 UI handoff §5, §7).

Owns: installing/uninstalling the three gna_pipeline seams around a run
(console.set_event_sink, console.set_confirm_handler,
scheduling.set_cancel_event), the in-memory SSE ring buffer (monotonic
`seq`, replay-from-`after`), the confirm bridge (publish `confirm_request`,
block on a threading.Event, 10-min timeout -> No), and the cancel latch
(sticky -- denies any confirm that arrives after Stop, including one raised
before the batch loop starts).

One run at a time, process-wide: a single `RunManager` instance
(`manager`, module-level below) backs every route. This module is the
"hub file" every route in this wave depends on -- built once, serially,
before the upload/read-only/settings routes (which only ever call methods
on `manager`, never touch its internals).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from gna_pipeline import cli, config, console, pipeline, scheduling

# Terminal run_state values (v2 handoff §7.2).
_TERMINAL_STATES = ("done", "interrupted", "declined", "error")


class RunConflict(Exception):
    """Raised by start_run() when a run is already active. Routes map this
    to HTTP 409."""


class RunManager:
    # Class attribute (not a bare literal in _on_confirm) so tests can
    # monkeypatch it down from 10 real minutes to exercise the timeout path.
    CONFIRM_TIMEOUT_S: float = 600.0

    def __init__(self) -> None:
        self._cv = threading.Condition()

        # Per-run state, (re)initialized fresh in start_run(). Typed here so
        # every attribute's shape is documented in one place.
        self._active: bool = False
        self._run_id: str | None = None
        self._kind: str | None = None
        self._run_counter: int = 0

        self._events: list[dict[str, Any]] = []
        self._seq: int = 0
        self._closed: bool = True  # no run has ever started

        self._cancel_event: threading.Event | None = None
        self._cancel_latched: bool = False

        self._pending_confirms: dict[str, dict[str, Any]] = {}
        self._confirm_counter: int = 0
        self._confirm_denied: bool = False

        self._outputs_emitted: bool = False

    # -----------------------------------------------------------------
    # Public state (read-only snapshots for GET /api/state etc.)
    # -----------------------------------------------------------------

    def run_snapshot(self) -> dict[str, Any] | None:
        with self._cv:
            if self._run_id is None:
                return None
            return {"active": self._active, "run_id": self._run_id, "kind": self._kind}

    def is_active(self) -> bool:
        with self._cv:
            return self._active

    # -----------------------------------------------------------------
    # Starting a run — kind in {"run", "deal-profile", "recover"}.
    # `kwargs` is passed straight to the matching gna_pipeline entry point
    # (see _run_worker); callers (routes_run.py) own building it correctly
    # per kind.
    # -----------------------------------------------------------------

    def start_run(self, kind: str, kwargs: dict[str, Any]) -> str:
        if kind not in ("run", "deal-profile", "recover"):
            raise ValueError(f"unknown run kind {kind!r}")
        with self._cv:
            if self._active:
                raise RunConflict("a run is already active")
            self._run_counter += 1
            run_id = f"run-{self._run_counter}"
            self._run_id = run_id
            self._kind = kind
            self._active = True
            self._events = []
            self._seq = 0
            self._closed = False
            self._cancel_event = threading.Event()
            self._cancel_latched = False
            self._pending_confirms = {}
            self._confirm_counter = 0
            self._confirm_denied = False
            self._outputs_emitted = False

        thread = threading.Thread(target=self._run_worker, args=(kind, kwargs), daemon=True)
        thread.start()
        return run_id

    def _run_worker(self, kind: str, kwargs: dict[str, Any]) -> None:
        cancel_event = self._cancel_event  # captured under the lock in start_run
        console.set_event_sink(self._sink_emit)
        console.set_confirm_handler(self._on_confirm)
        scheduling.set_cancel_event(cancel_event)

        self._emit("run_state", {"state": "running"})

        stderr_buf = io.StringIO()
        exit_code: int | None = None
        crash_message: str | None = None
        try:
            with contextlib.redirect_stderr(stderr_buf):
                if kind == "run":
                    exit_code = pipeline.run_pipeline(**kwargs)
                elif kind == "deal-profile":
                    exit_code = self._run_deal_profile(kwargs)
                else:  # "recover"
                    exit_code = self._run_recover(kwargs)
        except Exception as exc:  # noqa: BLE001 — a run must always resolve to a
            # terminal run_state, never leave the manager stuck "active" because
            # of an unhandled exception in the worker thread.
            crash_message = f"{type(exc).__name__}: {exc}"
        finally:
            console.set_event_sink(None)
            console.set_confirm_handler(None)
            scheduling.set_cancel_event(None)

        state_name, message = self._derive_final_state(
            kind,
            exit_code=exit_code,
            crash_message=crash_message,
            cancelled=(cancel_event is not None and cancel_event.is_set()),
            stderr_text=stderr_buf.getvalue().strip(),
        )
        payload: dict[str, Any] = {"state": state_name, "exit_code": exit_code}
        if message is not None:
            payload["message"] = message
        self._emit("run_state", payload)

        with self._cv:
            self._active = False
            self._closed = True
            self._cv.notify_all()

    def _derive_final_state(
        self,
        kind: str,
        *,
        exit_code: int | None,
        crash_message: str | None,
        cancelled: bool,
        stderr_text: str,
    ) -> tuple[str, str | None]:
        """Returns (state, message|None). `message` is only set for "error".

        Exit-code semantics differ per kind (each gna_pipeline entry point
        owns its own return-code convention; this does not invent a new one,
        just reads each one correctly):
          - "run" (pipeline.run_pipeline): 0 = success OR declined at the
            top-level Proceed gate (returns immediately, before stage11 ever
            runs -- existing behavior, unrelated to this UI); 1 = EITHER the
            missing-API-key error (before anything is written) OR stage11's
            Excel write failing (after everything else completed) --
            `_outputs_emitted` (set the instant stage11's `data("outputs",
            ...)` fires) is what tells those two apart; 2 = a scope/argument
            error; 130 = interrupted (already handled above via `cancelled`).
          - "deal-profile" (cli.cmd_deal_profile): 0 = success or declined-
            at-its-own-Proceed-gate; 1 = missing API key (nothing written);
            2 = scope error. No stage11-style "outputs" event exists for
            this command, so exit_code alone is unambiguous here.
          - "recover" (cli.cmd_recover): 0 = success; 1 = ONLY the Excel
            write failing (summary.json was already rebuilt first, same
            "done, but offer recover again" shape as a run's Excel-write
            failure); 2 = scope error. No confirm gate exists for recover at
            all, so `_confirm_denied` never fires for this kind.
        """
        if crash_message is not None:
            return "error", crash_message
        if cancelled:
            return "interrupted", None
        if self._confirm_denied:
            return "declined", None

        if kind == "run":
            ok = exit_code == 0 or self._outputs_emitted
        elif kind == "deal-profile":
            ok = exit_code == 0
        else:  # "recover"
            ok = exit_code in (0, 1)

        if ok:
            return "done", None
        return "error", stderr_text or "run failed before producing any output"

    def _run_deal_profile(self, kwargs: dict[str, Any]) -> int:
        ns = argparse.Namespace(
            workbook=kwargs["workbook"],
            quarters=kwargs.get("quarters"),
            months=kwargs.get("months"),
            min_usd=kwargs.get("min_usd"),
            dry_run=False,
            yes=False,
            no_fetch=kwargs.get("no_fetch", False),
            model=kwargs.get("model"),
        )
        return cli.cmd_deal_profile(ns)

    def _run_recover(self, kwargs: dict[str, Any]) -> int:
        ns = argparse.Namespace(
            workbook=kwargs["workbook"],
            months=kwargs.get("months"),
            min_usd=kwargs.get("min_usd"),
        )
        return cli.cmd_recover(ns)

    # -----------------------------------------------------------------
    # Event sink (installed as console._event_sink for the run's duration).
    # -----------------------------------------------------------------

    def _emit(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._cv:
            self._seq += 1
            entry = {"seq": self._seq, "ts": time.strftime("%H:%M:%S"), "type": event_type, "payload": payload}
            self._events.append(entry)
            self._cv.notify_all()
            return entry

    def _sink_emit(self, kind: str, payload: dict[str, Any]) -> None:
        self._emit(kind, payload)
        if kind == "data" and payload.get("kind") == "outputs":
            with self._cv:
                self._outputs_emitted = True

    # -----------------------------------------------------------------
    # Confirm bridge — console.confirm() calls this (via set_confirm_handler)
    # from the WORKER thread; it blocks there until an HTTP route answers it
    # or the 10-minute timeout elapses (safe default: No).
    # -----------------------------------------------------------------

    def _on_confirm(self, prompt: str) -> bool:
        with self._cv:
            if self._cancel_latched:
                # Stop was already pressed -- auto-deny immediately, including
                # a confirm raised during Phase 0 before any batch loop starts.
                self._confirm_denied = True
                return False
            self._confirm_counter += 1
            # Unguessable: a predictable id (e.g. "c1") could be blind-POSTed to
            # /api/run/confirm to approve a paid run without the operator. The
            # counter prefix stays for readable logs; the token is the security part.
            confirm_id = f"c{self._confirm_counter}-{secrets.token_urlsafe(8)}"
            event = threading.Event()
            self._pending_confirms[confirm_id] = {"event": event, "answer": False}

        self._emit("confirm_request", {"confirm_id": confirm_id, "prompt": prompt})
        got_answer = event.wait(timeout=self.CONFIRM_TIMEOUT_S)

        with self._cv:
            entry = self._pending_confirms.pop(confirm_id, None)
            latched = self._cancel_latched
        answer = bool(entry["answer"]) if (got_answer and entry is not None) else False
        if latched:
            answer = False
        if not answer:
            with self._cv:
                self._confirm_denied = True
        return answer

    def answer_confirm(self, confirm_id: str, answer: bool) -> bool:
        """Called from an HTTP route handler thread. Returns False if
        `confirm_id` is unknown or already answered/expired (stale UI
        state) -- the route treats that as a 404, not a 500."""
        with self._cv:
            entry = self._pending_confirms.get(confirm_id)
            if entry is None:
                return False
            entry["answer"] = bool(answer)
            entry["event"].set()
            return True

    # -----------------------------------------------------------------
    # Cancel — sticky latch (rule 5 of the v2 handoff's §5): once tripped,
    # every subsequent confirm auto-denies for the rest of this run.
    # -----------------------------------------------------------------

    def cancel(self) -> bool:
        with self._cv:
            if not self._active:
                return False
            self._cancel_latched = True
            cancel_event = self._cancel_event
            pending = list(self._pending_confirms.items())
        for _confirm_id, entry in pending:
            entry["answer"] = False
            entry["event"].set()
        if cancel_event is not None:
            cancel_event.set()
        self._emit("info", {"msg": "Stop requested -- in-flight batches will finish, then the run stops."})
        return True

    # -----------------------------------------------------------------
    # SSE replay — GET /api/run/events?after=N (v2 handoff §7.2).
    # -----------------------------------------------------------------

    def stream_events(self, after: int):
        """Yields buffered/live event dicts with seq > `after`, then closes
        once the run reaches a terminal state and every event up to that
        point has been sent. A browser refresh mid-run reattaches with
        `after` = the last seq it saw and rebuilds the rest from the buffer.

        Ends early (no more events) if a NEW run replaces this one's buffer
        entirely (start_run resets seq/events) — a stale reader must not
        silently wait forever on a cursor that can never be reached again."""
        with self._cv:
            run_id_at_start = self._run_id
        last_sent = after
        while True:
            with self._cv:
                self._cv.wait_for(
                    lambda: self._seq > last_sent or self._closed or self._run_id != run_id_at_start,
                    timeout=15,
                )
                if self._run_id != run_id_at_start:
                    return
                batch = [e for e in self._events if e["seq"] > last_sent]
                done = self._closed
            for entry in batch:
                last_sent = entry["seq"]
                yield entry
            if done:
                return


manager = RunManager()
