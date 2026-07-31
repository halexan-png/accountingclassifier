"""tests/test_gna_server.py — Wave 2 server: run-manager core + routes.

Two layers:
  1. RunManager unit tests against a monkeypatched `gna_pipeline.pipeline.
     run_pipeline` (a fake callable standing in for the real, heavy
     pipeline) -- proves the seam install/uninstall, SSE ring buffer,
     confirm bridge (answer / timeout / cancel-latch auto-deny), and
     run_state derivation (done / declined / interrupted / error / crash)
     without ever touching a real workbook or spending anything.
  2. A handful of route-shape tests via FastAPI's TestClient, using
     monkeypatched settings paths so a PUT never touches the real
     doctrine files.

No test here depends on a real workbook or ANTHROPIC_API_KEY -- that
coverage is a separate, non-committed live sandbox proof (real data is
gitignored and not guaranteed present in a fresh clone).
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from gna_pipeline import cli, console, pipeline, scheduling

from gna_server import routes_run, routes_settings
from gna_server.run_manager import RunConflict, RunManager
from gna_server.state import state as server_state


# ---------------------------------------------------------------------------
# RunManager unit tests
# ---------------------------------------------------------------------------

def _wait_inactive(manager: RunManager, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not manager.is_active():
            return
        time.sleep(0.01)
    raise AssertionError("run did not finish in time")


def _wait_for_pending_confirm(manager: RunManager, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = list(manager._pending_confirms.keys())
        if pending:
            return pending[0]
        time.sleep(0.01)
    raise AssertionError("no confirm_request arrived in time")


def _final_state(manager: RunManager) -> dict:
    terminal = [
        e for e in manager._events
        if e["type"] == "run_state" and e["payload"]["state"] != "running"
    ]
    assert terminal, "no terminal run_state event was emitted"
    return terminal[-1]["payload"]


@pytest.fixture
def manager(monkeypatch):
    """A fresh RunManager per test -- NOT the process-wide singleton, so
    tests never interleave. Seams (console/scheduling) are still the real
    process-global modules; each test's fake pipeline runs to completion
    (waited via _wait_inactive) before teardown, so no seam leaks out."""
    mgr = RunManager()
    yield mgr
    # Belt-and-suspenders: if a test failed mid-run, make sure the real
    # console/scheduling seams are never left installed for later tests.
    console.set_event_sink(None)
    console.set_confirm_handler(None)
    scheduling.set_cancel_event(None)


def test_done_state_and_event_ordering(manager):
    def fake_run_pipeline(**kwargs):
        console.section("Ingest")
        console.info("hello")
        console.data("outputs", {"excel": "classified.xlsx", "excel_ok": True})
        return 0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "run_pipeline", fake_run_pipeline)
        run_id = manager.start_run("run", {"workbook": "irrelevant"})
        assert run_id == "run-1"
        _wait_inactive(manager)

    assert _final_state(manager)["state"] == "done"
    seqs = [e["seq"] for e in manager._events]
    assert seqs == sorted(seqs) and seqs == list(range(1, len(seqs) + 1))
    kinds = [e["type"] for e in manager._events]
    assert kinds[0] == "run_state"  # "running", emitted first
    assert "section" in kinds and "info" in kinds and "data" in kinds


def test_declined_state_when_confirm_answered_no(manager):
    def fake_run_pipeline(**kwargs):
        if not console.confirm("Proceed? [y/N] "):
            return 0
        console.data("outputs", {"excel_ok": True})
        return 0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "run_pipeline", fake_run_pipeline)
        manager.start_run("run", {})
        confirm_id = _wait_for_pending_confirm(manager)
        assert manager.answer_confirm(confirm_id, False) is True
        _wait_inactive(manager)

    assert _final_state(manager)["state"] == "declined"


def test_done_state_when_confirm_answered_yes(manager):
    def fake_run_pipeline(**kwargs):
        if not console.confirm("Proceed? [y/N] "):
            return 0
        console.data("outputs", {"excel_ok": True})
        return 0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "run_pipeline", fake_run_pipeline)
        manager.start_run("run", {})
        confirm_id = _wait_for_pending_confirm(manager)
        assert manager.answer_confirm(confirm_id, True) is True
        _wait_inactive(manager)

    assert _final_state(manager)["state"] == "done"


def test_confirm_timeout_defaults_to_no(manager):
    def fake_run_pipeline(**kwargs):
        if not console.confirm("Proceed? [y/N] "):
            return 0
        console.data("outputs", {"excel_ok": True})
        return 0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "run_pipeline", fake_run_pipeline)
        mp.setattr(manager, "CONFIRM_TIMEOUT_S", 0.2)
        manager.start_run("run", {})
        _wait_inactive(manager, timeout=5.0)

    assert _final_state(manager)["state"] == "declined"


def test_cancel_latches_and_denies_pending_confirm(manager):
    reached_after_cancel = threading.Event()

    def fake_run_pipeline(**kwargs):
        # Simulate a worker loop that checks the cancel event, like
        # RateLimiter.acquire's poll loop does.
        for _ in range(200):
            if scheduling._cancel_requested():
                return 130
            time.sleep(0.01)
        # Should never get here in this test -- cancel() is called well
        # before 2s elapse.
        reached_after_cancel.set()
        return 0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "run_pipeline", fake_run_pipeline)
        manager.start_run("run", {})
        time.sleep(0.05)  # let the worker thread install the cancel event
        assert manager.cancel() is True
        _wait_inactive(manager)

    assert not reached_after_cancel.is_set()
    assert _final_state(manager)["state"] == "interrupted"

    # A confirm raised AFTER Stop (e.g. mid Phase 0) must auto-deny with no
    # confirm_request event at all -- the latch, not a real round trip.
    n_events_before = len(manager._events)
    answer = manager._on_confirm("late confirm")
    assert answer is False
    assert len(manager._events) == n_events_before  # no confirm_request emitted


def test_error_state_captures_stderr_for_console_bypassing_errors(manager):
    def fake_run_pipeline(**kwargs):
        import sys
        print("ERROR: invalid --months value", file=sys.stderr)
        return 2

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "run_pipeline", fake_run_pipeline)
        manager.start_run("run", {})
        _wait_inactive(manager)

    final = _final_state(manager)
    assert final["state"] == "error"
    assert "invalid --months value" in final["message"]


def test_crash_is_captured_not_left_hanging(manager):
    def fake_run_pipeline(**kwargs):
        raise RuntimeError("boom")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "run_pipeline", fake_run_pipeline)
        manager.start_run("run", {})
        _wait_inactive(manager)

    final = _final_state(manager)
    assert final["state"] == "error"
    assert "boom" in final["message"]
    assert manager.is_active() is False


def test_conflict_while_active(manager):
    release = threading.Event()

    def fake_run_pipeline(**kwargs):
        release.wait(timeout=5)
        return 0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "run_pipeline", fake_run_pipeline)
        manager.start_run("run", {})
        with pytest.raises(RunConflict):
            manager.start_run("run", {})
        release.set()
        _wait_inactive(manager)


def test_seams_uninstalled_after_run(manager):
    def fake_run_pipeline(**kwargs):
        return 0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "run_pipeline", fake_run_pipeline)
        manager.start_run("run", {})
        _wait_inactive(manager)

    # console.py / scheduling.py module-level seam state must be clear so a
    # later CLI-only (non-server) call behaves exactly as if the server had
    # never run.
    assert console._event_sink is None
    assert console._confirm_handler is None
    assert scheduling._cancel_event is None


def test_stream_events_replays_full_buffer_after_completion(manager):
    def fake_run_pipeline(**kwargs):
        console.info("a")
        console.info("b")
        console.data("outputs", {"excel_ok": True})
        return 0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pipeline, "run_pipeline", fake_run_pipeline)
        manager.start_run("run", {})
        _wait_inactive(manager)

    replayed = list(manager.stream_events(after=0))
    assert [e["seq"] for e in replayed] == [e["seq"] for e in manager._events]
    assert replayed[-1]["type"] == "run_state"
    assert replayed[-1]["payload"]["state"] == "done"


# ---------------------------------------------------------------------------
# Per-kind exit-code semantics (deal-profile / recover) — regression coverage
# for the "outputs_emitted only exists for kind=run" gap: cmd_deal_profile
# and cmd_recover never emit a `data("outputs", ...)` event (only
# pipeline.run_pipeline's stage11 does), so _derive_final_state must read
# each kind's exit code on its own terms, not via that signal.
# ---------------------------------------------------------------------------

def test_recover_kind_done_despite_exit_1_excel_write_failure(manager):
    def fake_cmd_recover(ns):
        return 1  # cmd_recover's ONLY exit-1 case: the Excel write failed

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "cmd_recover", fake_cmd_recover)
        manager.start_run("recover", {"workbook": "irrelevant"})
        _wait_inactive(manager)

    assert _final_state(manager)["state"] == "done"


def test_recover_kind_error_on_scope_exit_2(manager):
    def fake_cmd_recover(ns):
        import sys
        print("ERROR: invalid --months value", file=sys.stderr)
        return 2

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "cmd_recover", fake_cmd_recover)
        manager.start_run("recover", {"workbook": "irrelevant"})
        _wait_inactive(manager)

    final = _final_state(manager)
    assert final["state"] == "error"
    assert "invalid --months value" in final["message"]


def test_deal_profile_kind_error_on_missing_api_key_exit_1(manager):
    def fake_cmd_deal_profile(ns):
        return 1  # cmd_deal_profile's ONLY exit-1 case: missing API key

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "cmd_deal_profile", fake_cmd_deal_profile)
        manager.start_run("deal-profile", {"workbook": "irrelevant"})
        _wait_inactive(manager)

    assert _final_state(manager)["state"] == "error"


def test_deal_profile_kind_done_on_success(manager):
    def fake_cmd_deal_profile(ns):
        return 0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "cmd_deal_profile", fake_cmd_deal_profile)
        manager.start_run("deal-profile", {"workbook": "irrelevant"})
        _wait_inactive(manager)

    assert _final_state(manager)["state"] == "done"


# ---------------------------------------------------------------------------
# Route-shape tests (TestClient) — base_url is loopback so app.py's
# Host/Origin guard middleware doesn't reject every request outright.
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from gna_server.app import app
    return TestClient(app, base_url="http://127.0.0.1")


def test_get_state_shape(client):
    resp = client.get("/api/state")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("workbook", "api_key_present", "invoice_library", "run", "model_default", "ui_version"):
        assert key in body


def test_non_loopback_host_rejected(client):
    resp = client.get("/api/state", headers={"Host": "evil.example.com"})
    assert resp.status_code == 403


def test_ping_ok(client):
    resp = client.get("/api/ping")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_ping_is_exempt_from_idle_activity_but_state_is_not(client):
    """The liveness ping must NOT reset the idle-shutdown clock (or an open tab
    would keep the server alive forever), while a genuine request must. This
    pins both halves of app.py's _LIVENESS_PATHS exemption at the middleware
    level, since getting it wrong silently defeats either auto-shutdown or the
    timeout screen."""
    from gna_server import lifecycle

    # Pretend the server has been idle for a long time.
    lifecycle._last_activity = time.monotonic() - 1000.0
    client.get("/api/ping")
    assert lifecycle.seconds_since_activity() > 900.0  # ping left the clock alone

    client.get("/api/state")
    assert lifecycle.seconds_since_activity() < 5.0  # a real request reset it


def test_activity_beacon_resets_idle(client):
    """The /api/activity keep-alive beacon (sent while the operator is
    interacting on-screen) MUST reset the idle clock -- it's how mouse/keyboard
    activity that makes no other request keeps the session alive."""
    from gna_server import lifecycle

    lifecycle._last_activity = time.monotonic() - 1000.0
    resp = client.get("/api/activity")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert lifecycle.seconds_since_activity() < 5.0


def test_shutdown_refused_while_run_active(client, monkeypatch):
    """A deliberate "Close application" (POST /api/shutdown) must never kill a
    live (paid) run -- it returns 409 while a run is active, mirroring the idle
    watchdog's own gate (gna_server/lifecycle.should_shut_down)."""
    from gna_server import routes_lifecycle

    monkeypatch.setattr(routes_lifecycle.manager, "is_active", lambda: True)
    resp = client.post("/api/shutdown", json={})
    assert resp.status_code == 409


def test_shutdown_triggers_registered_exit_hook(client, monkeypatch):
    """With no run active, POST /api/shutdown fires the registered exit hook --
    the same should_exit flip __main__ wires for the watchdog -- and reports ok.
    Pins that the manual close path actually reaches the shutdown mechanism."""
    from gna_server import lifecycle, routes_lifecycle

    monkeypatch.setattr(routes_lifecycle.manager, "is_active", lambda: False)
    fired = {"n": 0}
    lifecycle.register_exit(lambda: fired.__setitem__("n", fired["n"] + 1))
    try:
        resp = client.post("/api/shutdown", json={})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert fired["n"] == 1
    finally:
        lifecycle.register_exit(None)  # never leak the hook into other tests


def test_shutdown_without_exit_hook_is_503(client, monkeypatch):
    """If no exit hook is registered (e.g. the TestClient here -- __main__.main()
    never ran), shutdown reports 503 rather than pretend the server is closing."""
    from gna_server import lifecycle, routes_lifecycle

    monkeypatch.setattr(routes_lifecycle.manager, "is_active", lambda: False)
    lifecycle.register_exit(None)
    resp = client.post("/api/shutdown", json={})
    assert resp.status_code == 503


def test_run_without_workbook_is_400(client):
    resp = client.post("/api/run", json={"kind": "run", "quarter": "2026Q1"})
    assert resp.status_code == 400


def test_run_request_user_deal_context_becomes_pipeline_override_kwarg(client, tmp_path, monkeypatch):
    """Proves the HTTP field name `user_deal_context` (what the Additional
    Context modal sends, see frontend/real-adapter.js) actually arrives at
    `pipeline.run_pipeline`'s `user_deal_context_override` kwarg (see
    routes_run._build_run_kwargs) -- the two names differ, so a rename on
    either side would silently break this without a test pinned on both."""
    before = server_state.workbook_path
    server_state.workbook_path = tmp_path / "workbook.xlsx"

    captured: dict = {}

    def fake_start_run(kind, kwargs):
        captured["kind"] = kind
        captured["kwargs"] = kwargs
        return "fake-run-id"

    monkeypatch.setattr(routes_run.manager, "start_run", fake_start_run)

    try:
        resp = client.post("/api/run", json={
            "kind": "run",
            "quarter": "2026Q1",
            "user_deal_context": "MARKER_FROM_HTTP_REQUEST_BODY",
        })
    finally:
        server_state.workbook_path = before

    assert resp.status_code == 200
    assert resp.json() == {"run_id": "fake-run-id"}
    assert captured["kind"] == "run"
    assert captured["kwargs"]["user_deal_context_override"] == "MARKER_FROM_HTTP_REQUEST_BODY"


def test_run_request_omitted_user_deal_context_is_none(client, tmp_path, monkeypatch):
    """The sibling of the test above: no Additional Context typed -> the
    override kwarg must be None, not missing or empty string, so
    pipeline.run_pipeline falls through to the on-disk loader unchanged."""
    before = server_state.workbook_path
    server_state.workbook_path = tmp_path / "workbook.xlsx"

    captured: dict = {}
    monkeypatch.setattr(
        routes_run.manager, "start_run",
        lambda kind, kwargs: captured.update(kwargs=kwargs) or "fake-run-id",
    )

    try:
        resp = client.post("/api/run", json={"kind": "run", "quarter": "2026Q1"})
    finally:
        server_state.workbook_path = before

    assert resp.status_code == 200
    assert captured["kwargs"]["user_deal_context_override"] is None


def test_docs_endpoint_reads_real_repo_file(client):
    resp = client.get("/api/docs/getting_started")
    assert resp.status_code == 200
    assert "markdown" in resp.json()
    assert len(resp.json()["markdown"]) > 0


def test_docs_all_seven_keys_resolve(client):
    for key in (
        "getting_started", "odyssey", "pipeline_overview", "invoice_rules",
        "context_tiering", "input_format", "risk_notes",
    ):
        resp = client.get(f"/api/docs/{key}")
        assert resp.status_code == 200, key
        assert len(resp.json()["markdown"]) > 0, key


def test_docs_old_keys_retired(client):
    assert client.get("/api/docs/quickstart").status_code == 404
    assert client.get("/api/docs/how_it_works").status_code == 404


def test_docs_promotes_plain_text_headings(client):
    markdown = client.get("/api/docs/context_tiering").json()["markdown"]
    assert markdown.startswith("# HOW THE CLASSIFIER DECIDES WHAT TO READ, AND IN WHAT ORDER")
    assert "## THE FIVE THINGS THE MODEL IS SHOWN, IN ORDER" in markdown
    # The dashes divider line under each promoted heading must be consumed,
    # not just left dangling under the new '##'.
    assert not any(line.strip() and set(line.strip()) == {"-"} for line in markdown.splitlines())


def test_docs_leaves_real_markdown_untouched(client):
    markdown = client.get("/api/docs/input_format").json()["markdown"]
    assert markdown.startswith("# Intended input file")
    assert "## 1. The G&A workbook" in markdown


def test_docs_unknown_key_404(client):
    resp = client.get("/api/docs/nope")
    assert resp.status_code == 404


def test_download_sample_workbooks(client):
    assert client.get("/api/download/sample_ga").status_code == 200
    assert client.get("/api/download/sample_at").status_code == 200


def test_static_frontend_is_never_cached(client):
    # The no-build ES-module frontend must be served with Cache-Control: no-store
    # so a browser can't keep running a stale app.js/guide.js against a restarted
    # backend (the staleness that made the rebuilt Guide's tabs/downloads look
    # broken). Guards the app.py middleware that stamps this on static GETs.
    resp = client.get("/app.js")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"


def test_api_responses_are_not_forced_no_store(client):
    # The no-store stamp is scoped to the static frontend only -- /api/* handlers
    # own their own cache semantics and must not be blanket-tagged by the same
    # middleware.
    resp = client.get("/api/docs/odyssey")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") != "no-store"


def test_results_endpoint_never_lists_static_samples(client):
    # sample_ga/sample_at are real, always-present files on disk (unlike
    # classified.xlsx, a run artifact) -- a regression here would make every
    # completed run's Output screen show two extra, unrelated download chips.
    artifacts = client.get("/api/results").json()["artifacts"]
    keys = {a["key"] for a in artifacts}
    assert "sample_ga" not in keys
    assert "sample_at" not in keys


def test_settings_roundtrip_never_touches_real_doctrine_file(client, tmp_path, monkeypatch):
    fake_path = tmp_path / "classifier.md"
    fake_path.write_text("original", encoding="utf-8")
    monkeypatch.setitem(routes_settings._PATHS, "classifier", fake_path)

    got = client.get("/api/settings/classifier")
    assert got.status_code == 200
    assert got.json()["content"] == "original"

    put = client.put("/api/settings/classifier", json={"content": "new doctrine text"})
    assert put.status_code == 200
    assert fake_path.read_text(encoding="utf-8") == "new doctrine text"


def test_settings_roundtrip_companynorm_never_touches_real_file(client, tmp_path, monkeypatch):
    """Mirrors the classifier round-trip above for the Stream B company-norms
    settings tab (routes_settings._PATHS["companynorm"] -> config.COMPANY_NORMS_MD)."""
    fake_path = tmp_path / "companynorm.md"
    fake_path.write_text("original norms", encoding="utf-8")
    monkeypatch.setitem(routes_settings._PATHS, "companynorm", fake_path)

    got = client.get("/api/settings/companynorm")
    assert got.status_code == 200
    assert got.json()["content"] == "original norms"

    put = client.put("/api/settings/companynorm", json={"content": "Acme Corp is routine."})
    assert put.status_code == 200
    assert fake_path.read_text(encoding="utf-8") == "Acme Corp is routine."


def test_workbook_upload_rejects_bad_extension(client):
    resp = client.post(
        "/api/workbook",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 400
