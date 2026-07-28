"""tests/test_graph.py — Microsoft Graph (OneDrive/SharePoint invoice links):
graph_fetch's host detection, invoice_read's routing branch, graph_auth's
token-cache plumbing, and the /api/graph/* routes.

Never touches a real Microsoft endpoint or opens a browser: graph_auth's
network-facing calls (connect_interactive, acquire_token_silent's network
path) and graph_fetch.fetch_via_graph are monkeypatched wherever a test would
otherwise need one. The one real MSAL call exercised here
(get_token_silent with an empty cache/no accounts) is local-only -- MSAL
reads accounts from the cache, no network round trip.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from gna_pipeline import config, graph_auth, graph_fetch, invoice_read

import gna_server.routes_graph as routes_graph


@pytest.fixture(autouse=True)
def _reset_graph_auth_singleton(monkeypatch, tmp_path):
    """graph_auth caches its PublicClientApplication/token cache at module
    scope (see graph_auth._get_app) -- reset both and point the cache path
    at a throwaway file so tests never read/write a real cache."""
    monkeypatch.setattr(graph_auth, "_app", None)
    monkeypatch.setattr(graph_auth, "_cache", None)
    monkeypatch.setattr(config, "GRAPH_TOKEN_CACHE", tmp_path / "graph_token_cache.bin")
    yield


# ---------------------------------------------------------------------------
# graph_fetch.is_graph_url — pure host detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://contoso-my.sharepoint.com/personal/user/Documents/invoice.pdf",
    "https://contoso.sharepoint.com/sites/team/Shared%20Documents/invoice.pdf",
    "https://1drv.ms/b/s!AbCdEf",
    "https://onedrive.live.com/download?cid=1234",
])
def test_is_graph_url_true(url):
    assert graph_fetch.is_graph_url(url) is True


@pytest.mark.parametrize("url", [
    "https://portal.avidxchange.com/invoices/12345.pdf",
    "https://example.com/invoice.pdf",
    "",
    None,
    "not a url at all",
])
def test_is_graph_url_false(url):
    assert graph_fetch.is_graph_url(url) is False


# ---------------------------------------------------------------------------
# graph_auth — cache/account plumbing, no network
# ---------------------------------------------------------------------------

def test_get_token_silent_none_when_never_connected():
    assert graph_auth.get_token_silent() is None
    assert graph_auth.is_connected() is False


# ---------------------------------------------------------------------------
# invoice_read routing — a OneDrive/SharePoint URL goes through graph_fetch
# ONLY when a token is available. A disconnected operator gets a clear,
# distinct error instead of falling through to the anonymous path: that path
# sends the raw, unencoded SharePoint URL straight to urllib, which throws a
# misleading low-level error ("URL can't contain control characters") that
# has nothing to do with the real problem (auth). Never silently degrade.
# ---------------------------------------------------------------------------

_ONEDRIVE_URL = "https://contoso.sharepoint.com/sites/team/Shared Documents/invoice.pdf"


class _FakeResponse:
    def __init__(self, body: bytes, content_type: str):
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_graph_url_errors_clearly_when_not_configured(monkeypatch):
    monkeypatch.setattr(config, "graph_configured", lambda: False)
    monkeypatch.setattr(graph_auth, "get_token_silent", lambda: None)

    def _fail_urlopen(req, timeout=None):
        raise AssertionError("must not fall through to the anonymous fetch path")

    monkeypatch.setattr(invoice_read.urllib.request, "urlopen", _fail_urlopen)

    result = invoice_read.fetch_invoice_url(_ONEDRIVE_URL)
    assert result["kind"] == "error"
    assert result["error"].startswith("graph_not_configured")


def test_graph_url_errors_clearly_when_configured_but_no_token(monkeypatch):
    monkeypatch.setattr(config, "graph_configured", lambda: True)
    monkeypatch.setattr(graph_auth, "get_token_silent", lambda: None)

    def _fail_urlopen(req, timeout=None):
        raise AssertionError("must not fall through to the anonymous fetch path")

    monkeypatch.setattr(invoice_read.urllib.request, "urlopen", _fail_urlopen)

    result = invoice_read.fetch_invoice_url(_ONEDRIVE_URL)
    assert result["kind"] == "error"
    assert result["error"].startswith("graph_not_connected")


def test_graph_url_uses_graph_fetch_when_connected(monkeypatch):
    monkeypatch.setattr(graph_auth, "get_token_silent", lambda: "fake-token")

    def _fake_fetch_via_graph(url, token, *, timeout_s=10.0):
        assert url == _ONEDRIVE_URL
        assert token == "fake-token"
        return b"%PDF-1.4 fake", "application/pdf", None

    monkeypatch.setattr(invoice_read.graph_fetch, "fetch_via_graph", _fake_fetch_via_graph)
    monkeypatch.setattr(
        invoice_read, "_read_pdf",
        lambda raw, pages: ("extracted text", b"trimmed-pdf-bytes", "1-1", "vision", 1, 4200),
    )

    result = invoice_read.fetch_invoice_url(_ONEDRIVE_URL)
    assert result["kind"] == "pdf"
    assert result["source"] == "url"
    assert result["path_or_url"] == _ONEDRIVE_URL
    assert result["read_path"] == "vision"
    assert result["est_input_tokens"] == 4200


def test_graph_url_error_when_graph_fetch_fails(monkeypatch):
    monkeypatch.setattr(graph_auth, "get_token_silent", lambda: "fake-token")
    monkeypatch.setattr(
        invoice_read.graph_fetch, "fetch_via_graph",
        lambda url, token, **kw: (None, "", "graph_metadata_http_403"),
    )

    result = invoice_read.fetch_invoice_url(_ONEDRIVE_URL)
    assert result["kind"] == "error"
    assert result["error"] == "graph_metadata_http_403"


def test_non_graph_url_never_touches_graph_auth(monkeypatch):
    """An AvidXchange (or any non-Microsoft) URL must never even ask
    graph_auth for a token -- proves the routing branch is host-gated, not
    just token-gated."""
    def _boom():
        raise AssertionError("graph_auth.get_token_silent should not be called")

    monkeypatch.setattr(graph_auth, "get_token_silent", _boom)

    def _fake_urlopen(req, timeout=None):
        return _FakeResponse(b"%PDF-1.4 fake", "application/pdf")

    monkeypatch.setattr(invoice_read.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(
        invoice_read, "_read_pdf",
        lambda raw, pages: ("text", b"pdf", "1-1", "vision", 1, 100),
    )

    result = invoice_read.fetch_invoice_url("https://portal.avidxchange.com/invoices/12345.pdf")
    assert result["kind"] == "pdf"


# ---------------------------------------------------------------------------
# gna_server/routes_graph.py — /api/graph/connect + /api/graph/status
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from gna_server.app import app
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture(autouse=True)
def _reset_routes_graph_state(monkeypatch):
    monkeypatch.setattr(routes_graph, "_status", "idle")
    monkeypatch.setattr(routes_graph, "_error", None)
    yield


def test_graph_status_idle_when_never_connected(client, monkeypatch):
    monkeypatch.setattr(graph_auth, "is_connected", lambda: False)
    resp = client.get("/api/graph/status")
    assert resp.status_code == 200
    assert resp.json() == {"status": "idle", "error": None}


def test_graph_status_reports_connected_from_a_prior_cached_token(client, monkeypatch):
    """Server restarted but the token cache on disk is still good -- status
    should read as connected without a fresh click/thread."""
    monkeypatch.setattr(graph_auth, "is_connected", lambda: True)
    resp = client.get("/api/graph/status")
    assert resp.json() == {"status": "connected", "error": None}


class _NoOpThread:
    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        pass  # never actually runs -- keeps status pinned at "connecting"


class _FakeThreadingModule:
    Thread = _NoOpThread


def test_post_connect_returns_connecting_and_dedupes(client, monkeypatch):
    monkeypatch.setattr(routes_graph, "threading", _FakeThreadingModule())

    r1 = client.post("/api/graph/connect", json={})
    assert r1.status_code == 200
    assert r1.json() == {"status": "connecting", "error": None}

    # A second click while already connecting must not spawn another worker --
    # same response, no crash, no re-entry into the "spawn" branch.
    r2 = client.post("/api/graph/connect", json={})
    assert r2.json() == {"status": "connecting", "error": None}


def test_connect_worker_marks_connected_on_success(monkeypatch):
    monkeypatch.setattr(graph_auth, "connect_interactive", lambda: (True, None))
    routes_graph._connect_worker()
    assert routes_graph._status == "connected"
    assert routes_graph._error is None


def test_connect_worker_marks_error_on_failure(monkeypatch):
    monkeypatch.setattr(graph_auth, "connect_interactive", lambda: (False, "AADSTS_something"))
    routes_graph._connect_worker()
    assert routes_graph._status == "error"
    assert routes_graph._error == "AADSTS_something"


def test_connect_then_status_reflects_worker_result_end_to_end(client, monkeypatch):
    """Real threading.Thread (not the no-op fake), monkeypatched
    connect_interactive returning instantly -- the closest thing to the real
    connect->poll flow without a browser or network."""
    monkeypatch.setattr(graph_auth, "connect_interactive", lambda: (True, None))

    resp = client.post("/api/graph/connect", json={})
    assert resp.json()["status"] == "connecting"

    deadline = time.monotonic() + 2.0
    status = None
    while time.monotonic() < deadline:
        status = client.get("/api/graph/status").json()
        if status["status"] != "connecting":
            break
        time.sleep(0.02)
    assert status == {"status": "connected", "error": None}
