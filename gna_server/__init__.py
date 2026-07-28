"""gna_server — local FastAPI wrapper around the finalized gna_pipeline backend.

Loopback-only (127.0.0.1), single operator, single machine. Owns no
classification logic of its own: every route is a thin translation between
HTTP/SSE and the pipeline's existing entry points (pipeline.run_pipeline,
cli.cmd_deal_profile, cli.cmd_recover) and seams (console.py's event sink /
confirm handler, scheduling.py's cancel event). See
handoff/HANDOFF_2026-07-17_ui_build_orchestrator_v2.md sections 5 and 7 for
the full architecture and API/SSE contract this package builds to.
"""
