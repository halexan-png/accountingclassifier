"""app.py — assembles the FastAPI app: routers + loopback security guard +
best-effort static frontend mount (v2 UI handoff §5, §9).

Security model (§9): this app must never be reachable from anything but the
machine it runs on. Binding to 127.0.0.1 (done by the entrypoint, not here)
is necessary but not sufficient -- any process on the machine, or a website
open in another browser tab, can still POST to a loopback port. The
middleware below is the cheap, v1-appropriate mitigation the handoff
specifies: reject any request whose Host header isn't loopback, reject any
Origin header that isn't loopback, and require a real JSON/multipart body on
every state-changing request (defeats simple no-preflight cross-origin
POSTs). Full login/auth is explicitly deferred (operator decision) -- not a
gap in this pass.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import (
    lifecycle,
    routes_graph,
    routes_lifecycle,
    routes_readonly,
    routes_run,
    routes_settings,
    routes_upload,
)

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_STATE_CHANGING_METHODS = ("POST", "PUT", "PATCH", "DELETE")
# Paths that must NOT count as activity for the idle auto-shutdown watchdog
# (lifecycle.py). The frontend polls /api/ping purely to detect that the server
# is still up; if that poll reset the idle timer, an open tab would keep the
# server alive forever and it could never time out (and never show the
# "session timed out" screen). Every OTHER request is genuine operator
# activity and does reset the timer.
_LIVENESS_PATHS = {"/api/ping"}

app = FastAPI(title="G&A Classifier UI")


@app.middleware("http")
async def loopback_guard(request: Request, call_next):
    if request.url.hostname not in LOOPBACK_HOSTS:
        return JSONResponse({"detail": "loopback only"}, status_code=403)

    # A served (loopback) request counts as activity -- resets the idle
    # auto-shutdown countdown (lifecycle.py). Placed after the host check so a
    # rejected non-loopback probe can't keep the server alive; the liveness
    # ping is exempt (see _LIVENESS_PATHS) so an open tab still times out.
    if request.url.path not in _LIVENESS_PATHS:
        lifecycle.touch()

    origin = request.headers.get("origin")
    if origin:
        origin_host = urlsplit(origin).hostname
        if origin_host not in LOOPBACK_HOSTS:
            return JSONResponse({"detail": "invalid origin"}, status_code=403)

    if request.method in _STATE_CHANGING_METHODS:
        content_type = request.headers.get("content-type", "")
        if not (content_type.startswith("application/json") or content_type.startswith("multipart/form-data")):
            return JSONResponse({"detail": "unsupported content-type"}, status_code=415)

    response = await call_next(request)

    # No-build ES-module frontend (app.js + its screen imports) served as static
    # files: browsers cache `type="module"` scripts aggressively and a plain
    # refresh does not reliably re-fetch them, so after a code change an operator
    # can keep running a STALE UI against a freshly-restarted backend -- the exact
    # failure mode behind the "Guide tabs don't respond / sample download isn't a
    # usable file after a rebuild" report. This is a single-user loopback app with
    # no CDN and no performance budget to protect; tell the browser never to cache
    # the frontend so every relaunch (even a plain tab refresh) picks up the
    # current code. /api/* responses set their own semantics and are left alone.
    if request.method == "GET" and not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"

    return response


app.include_router(routes_run.router)
app.include_router(routes_upload.router)
app.include_router(routes_readonly.router)
app.include_router(routes_settings.router)
app.include_router(routes_graph.router)
app.include_router(routes_lifecycle.router)

# Best-effort: Wave 1B (frontend/) may not exist yet when this app is
# imported (e.g. under pytest, or before the frontend wave lands). Mounted
# last so it never shadows an /api/* route, and only if the directory is
# actually there -- StaticFiles(check_dir=True) would otherwise raise at
# import time.
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
