"""tests/test_event_serialization.py — the SSE progress stream must never carry
non-JSON-serializable values.

Regression for the deal-profile crash: Phase-0 stats were emitted with their
internal `work_items` list, whose invoice results hold raw PDF `pdf_bytes`, so
`json.dumps` on the SSE event 500-looped the whole run. Two guards: (1) the
event strips `work_items`; (2) the SSE serializer degrades gracefully on any
stray non-serializable value.
"""

from __future__ import annotations

import json

from gna_pipeline import console, pipeline, prep
from gna_server import routes_run


def test_phase0_event_excludes_workitems_and_is_serializable():
    captured: list[tuple[str, dict]] = []
    console.set_event_sink(lambda kind, payload: captured.append((kind, payload)))
    try:
        stats0 = prep._empty_stats()
        stats0["had_invoice_yes"] = 1
        # A work item whose invoice was read as vision -> holds raw PDF bytes.
        stats0["work_items"] = [
            {"packet": {"row_idx": 5}, "invoice": {"kind": "pdf", "pdf_bytes": b"%PDF-1.4 raw"}}
        ]
        pipeline.print_phase0_stats(stats0)
    finally:
        console.set_event_sink(None)

    events = [p for k, p in captured if k == "data" and p.get("kind") == "phase0_stats"]
    assert events, "a phase0_stats data event was emitted"
    payload = events[0]["payload"]
    assert "work_items" not in payload            # the byte-carrying list is gone
    assert payload["had_invoice_yes"] == 1        # the scalar counts survive
    json.dumps(events[0])                          # whole event now serializes


def test_sse_serializer_survives_stray_bytes():
    entry = {"seq": 8, "type": "data", "payload": {"kind": "x", "raw": b"\x00\x01pdf"}}
    out = json.dumps(entry, default=routes_run._json_safe)  # must not raise
    assert "bytes" in out                          # bytes collapsed to a placeholder
