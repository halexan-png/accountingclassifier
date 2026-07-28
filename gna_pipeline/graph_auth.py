"""graph_auth.py — MSAL delegated auth for Microsoft Graph (OneDrive/SharePoint
invoice links). Optional layer: invoice_read only calls into this when a
Graph-hosted invoice_url is seen (see graph_fetch.is_graph_url); every other
URL (AvidXchange, etc.) never touches this module.

Delegated, no client secret — config.GRAPH_CLIENT_ID is a "public client" app
registration, so msal.PublicClientApplication is the right MSAL type.
connect_interactive() opens the operator's default browser once; the
resulting token (+ refresh token) is cached to config.GRAPH_TOKEN_CACHE so a
later call reuses it silently via acquire_token_silent() — no repeat browser
prompt until the refresh token itself expires.

Never raises: every public function here returns None/False on failure, so a
broken network, a declined consent, or a stale cache all degrade to "not
connected" — exactly the state before this feature existed. invoice_read's
existing anonymous-fetch path (and its login-page detection) is what actually
runs in that case, unchanged.
"""

from __future__ import annotations

import logging
import threading

import msal

from gna_pipeline import config

logger = logging.getLogger("gna.graph_auth")

_lock = threading.Lock()
_cache: msal.SerializableTokenCache | None = None
_app: msal.PublicClientApplication | None = None


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if config.GRAPH_TOKEN_CACHE.is_file():
        try:
            cache.deserialize(config.GRAPH_TOKEN_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass  # corrupt/unreadable cache — start fresh, never crash
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if not cache.has_state_changed:
        return
    try:
        config.GRAPH_TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        config.GRAPH_TOKEN_CACHE.write_text(cache.serialize(), encoding="utf-8")
    except OSError:
        logger.warning("graph_auth: could not persist token cache", exc_info=True)


def _get_app() -> tuple[msal.PublicClientApplication, msal.SerializableTokenCache]:
    global _app, _cache
    with _lock:
        if _app is None:
            _cache = _load_cache()
            _app = msal.PublicClientApplication(
                config.GRAPH_CLIENT_ID, authority=config.GRAPH_AUTHORITY, token_cache=_cache,
            )
        return _app, _cache


def get_token_silent() -> str | None:
    """Best-effort cached/refreshed access token. Never prompts, never raises.
    None if never connected, or the cached refresh token has itself expired
    (~90 days of inactivity) — the operator then needs one more
    connect_interactive() call."""
    if not config.graph_configured():
        return None
    app, cache = _get_app()
    accounts = app.get_accounts()
    if not accounts:
        return None
    try:
        result = app.acquire_token_silent(config.GRAPH_SCOPES, account=accounts[0])
    except Exception:  # noqa: BLE001 — a reader must never raise
        logger.warning("graph_auth: silent token refresh failed", exc_info=True)
        return None
    finally:
        _save_cache(cache)
    if result and "access_token" in result:
        return result["access_token"]
    return None


def is_connected() -> bool:
    if not config.graph_configured():
        return False
    return get_token_silent() is not None


def connect_interactive() -> tuple[bool, str | None]:
    """Blocking one-time delegated sign-in: opens the operator's default
    browser and waits for it to complete. Returns (ok, error_message|None).

    Must be called from a background thread, never a request-handler thread
    — this can take minutes waiting on the operator (see
    gna_server/routes_graph.py)."""
    if not config.graph_configured():
        return False, "Microsoft Graph is not configured (set GRAPH_TENANT_ID and GRAPH_CLIENT_ID in .env)"
    app, cache = _get_app()
    try:
        # timeout=None (MSAL's default) means "wait indefinitely" -- a closed
        # tab or a sign-in that never redirects back to localhost would then
        # block this thread forever, leaving routes_graph.py's status stuck
        # on "connecting" with no way to reach "error". Bound it instead.
        # prompt="select_account" -- otherwise Azure AD silently re-signs the
        # operator into whatever account has an active session in the system
        # browser, with no way to pick a different one on reconnect.
        result = app.acquire_token_interactive(
            config.GRAPH_SCOPES, timeout=300, prompt="select_account"
        )
    except Exception as e:  # noqa: BLE001 — never raise into the caller thread
        logger.warning("graph_auth: interactive sign-in failed", exc_info=True)
        return False, str(e)
    finally:
        _save_cache(cache)
    if result and "access_token" in result:
        return True, None
    err = (result or {}).get("error_description") or (result or {}).get("error") or "sign-in did not complete"
    return False, err
