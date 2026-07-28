"""graph_fetch.py — resolve a OneDrive/SharePoint sharing URL to raw bytes via
Microsoft Graph, using a delegated access token from graph_auth. Every other
invoice_url host is untouched by this module; invoice_read._fetch_invoice_url
is the only caller, and only for a URL where is_graph_url is True.

Trust-boundary note: this module only fetches bytes + a content-type — it does
NOT decide what counts as a readable invoice. That decision (is_pdf / html /
text / login-page scan) stays centralized in invoice_read.py, applied
identically whether the bytes came from here or the plain anonymous path.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from gna_pipeline import config

_GRAPH_HOST_SUFFIXES = ("sharepoint.com",)
_GRAPH_HOSTS_EXACT = {"1drv.ms", "onedrive.live.com"}

_SHARES_METADATA_URL = "https://graph.microsoft.com/v1.0/shares/{share_id}/driveItem"


def is_graph_url(url: str | None) -> bool:
    """True for a OneDrive-personal, OneDrive-for-Business, or SharePoint
    sharing link — the hosts an anonymous GET cannot read (Microsoft
    redirects to a login page instead of the file)."""
    if not url:
        return False
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    if host in _GRAPH_HOSTS_EXACT:
        return True
    return any(host.endswith(suffix) for suffix in _GRAPH_HOST_SUFFIXES)


def _encode_share_url(url: str) -> str:
    """Graph's base64url "u!" share-id encoding (Graph API: 'Get file or
    folder metadata from a sharing URL')."""
    b64 = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
    return f"u!{b64}"


def fetch_via_graph(
    url: str, access_token: str, *, timeout_s: float = 10.0
) -> tuple[bytes | None, str, str | None]:
    """Returns (raw_bytes, content_type, error_message). raw_bytes is None on
    failure. Never raises — same contract as invoice_read's own fetch helpers.

    Two Graph calls: metadata (to resolve the share link into a driveItem and
    get a temporary download URL), then a plain GET on that download URL. The
    download URL is a pre-authenticated, short-lived link — it must NOT carry
    the Authorization header (Graph documents this; sending one can get the
    request rejected)."""
    share_id = _encode_share_url(url)
    meta_req = urllib.request.Request(
        _SHARES_METADATA_URL.format(share_id=share_id),
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(meta_req, timeout=timeout_s) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return None, "", f"graph_metadata_http_{e.code}"
    except Exception as e:  # noqa: BLE001 — a reader must never raise
        return None, "", f"graph_metadata_failed: {e}"

    download_url = meta.get("@microsoft.graph.downloadUrl")
    if not download_url:
        return None, "", "graph_no_download_url"

    try:
        with urllib.request.urlopen(download_url, timeout=timeout_s) as resp:
            content_type = (resp.headers.get("Content-Type", "") or "").lower()
            raw = resp.read(config.INVOICE_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        return None, "", f"graph_download_http_{e.code}"
    except Exception as e:  # noqa: BLE001
        return None, "", f"graph_download_failed: {e}"

    return raw, content_type, None
