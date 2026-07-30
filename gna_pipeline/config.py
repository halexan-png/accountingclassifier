"""config.py — paths, model, token budget, rate limits.

Owns ALL configuration: filesystem paths, model IDs, numeric tuning constants,
and rate-limit loading. No business logic; the only IO is reading
rules/rate_limits.json and environment variables.

Launch via run_gna.ps1, which pins the Python 3.12 interpreter:

    & "$env:LOCALAPPDATA\\Programs\\Python\\Python312\\python.exe" -m gna_pipeline <cmd>
"""

from __future__ import annotations

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# DATA_ROOT — the external home for ALL business data (source workbook,
# invoices, deal profile, results/outputs, company norms, deal context). Set
# GNA_DATA_ROOT to a folder OUTSIDE this repo so real financial data is never
# stored inside the repo tree. Who sets it:
#   - UI flow (Start.cmd -> launch_ui.ps1 -> gna_server): __main__.py points it
#     at a throwaway temp workspace, wiped on exit -- nothing persists on disk.
#   - CLI flow (run_gna.ps1 -> gna_pipeline) and the pytest suite: NOT set, so
#     it falls back to REPO_ROOT below and CLI runs keep the exact same paths
#     they always had (behavior byte-identical, no test churn). This is also why
#     a CLI Graph login writes graph_token_cache.bin into the repo root (it's
#     gitignored). Pin GNA_DATA_ROOT for CLI runs if you want that out of the repo.
#
# Read once at import time: whoever sets the env var does so before the Python
# process starts (or, for the UI, before gna_server.app is imported), and
# gna_server's route modules freeze these paths into module-level dicts at
# import, so the value must be resolved here, not lazily.
#
# PROGRAM config (doctrines, rate_limits, product docs) deliberately stays on
# REPO_ROOT below — it is versioned, code-adjacent policy that ships with the
# repo, not per-run business data.
# ---------------------------------------------------------------------------
_data_root_env = os.environ.get("GNA_DATA_ROOT", "").strip()
DATA_ROOT = Path(_data_root_env).expanduser().resolve() if _data_root_env else REPO_ROOT

# ---------------------------------------------------------------------------
# Paths — business data derives from DATA_ROOT (external when GNA_DATA_ROOT is
# set); program config derives from REPO_ROOT. Nothing else hardcodes a path.
# ---------------------------------------------------------------------------
DATA_IN = DATA_ROOT / "data" / "input"
DATA_OUT = DATA_ROOT / "data" / "output"

# The non-technical operator's surface: everything they touch by hand lives
# here (source workbook, quarter deal notes, and where results land), so they
# never need to open data/ or gna_pipeline/. Everything else in this module
# stays put -- resume-state machinery (RESULTS_JSONL) and the deal-profile
# store are internal plumbing, not something an operator edits.
WORKSPACE_DIR = DATA_ROOT / "workspace"

# Stage-scoped folders. dealprofile lives under data/input/, not data/output/
# -- it's operator-editable deal context, not a generated artifact, so it
# belongs alongside the other input/ context files. Each stage owns one
# folder holding BOTH its artifacts and its own
# results.jsonl resume state, so a stage's state lives and dies together.
# Resetting a stage = deleting its folder:
#   - delete data/input/dealprofile/  -> the next sweep re-sweeps every M&A
#     row and rebuilds the profile from scratch (previously, deleting just
#     the profile JSON left the sweep's resume records behind in a shared
#     results.jsonl, so a "rebuilt" profile silently lost the vocabulary of
#     every already-swept row)
#   - delete data/output/results/     -> the next run reclassifies every row
#     from scratch (the deal profile is untouched)
DEAL_PROFILE_DIR = DATA_IN / "dealprofile"
DEAL_RESULTS_JSONL = DEAL_PROFILE_DIR / "results.jsonl"
DEAL_PROFILE_JSON = DEAL_PROFILE_DIR / "quarter_deal_profile.json"
DEAL_PROFILE_CONTEXT_TXT = DEAL_PROFILE_DIR / "deal_profile_context.txt"

RESULTS_DIR = DATA_OUT / "results"
RESULTS_JSONL = RESULTS_DIR / "results.jsonl"

# Operator-facing outputs live under workspace/, not data/output/ — these are
# the only two artifacts a non-technical operator ever opens. RESULTS_JSONL
# above stays in data/output/results/ on purpose: it's Phase-2 resume state,
# not something an operator reads, and `recover` rebuilds the two files below
# from it (see cli.cmd_recover) at zero API cost any time they're deleted.
WORKSPACE_RESULTS_DIR = WORKSPACE_DIR / "results"
SUMMARY_JSON = WORKSPACE_RESULTS_DIR / "summary.json"
CLASSIFIED_XLSX = WORKSPACE_RESULTS_DIR / "classified.xlsx"

# Default output of the two-file `run-q2` flatten step (the merged A&T + G&A
# flat workbook the rest of the pipeline then reads). Lives in workspace/ so a
# later `recover`/re-run finds it exactly like any other source workbook.
Q2_FLAT_XLSX = WORKSPACE_DIR / "q2_flat.xlsx"

RULES_DIR = REPO_ROOT / "rules"
RATE_LIMITS_JSON = RULES_DIR / "rate_limits.json"

# Single operator-authored deal-context file (this quarter's deal notes),
# loaded whole by deal_profile.load_human_deals_md and injected into BOTH the
# Phase-1 sweep and Phase-2 classify prompts. Lives in workspace/ so it sits
# right next to the workbook the operator drops there. Missing or empty file
# = no context (never fabricated); see load_human_deals_md's docstring for
# the word-count cap that guards against an unbounded prompt.
#
# Renamed 2026-07-17 from workspace/deal_context.md (the UI's "Additional
# Context" field is also called User Deal Context — the file name now
# matches). load_human_deals_md falls back to DEAL_CONTEXT_MD_LEGACY when
# DEAL_CONTEXT_MD is absent, so an existing quarter's notes under the old
# name aren't silently dropped.
DEAL_CONTEXT_MD = WORKSPACE_DIR / "user_deal_context.md"
DEAL_CONTEXT_MD_LEGACY = WORKSPACE_DIR / "deal_context.md"
DEAL_CONTEXT_WORD_CAP = 2500   # hard cap: refuse to load past this (operator error)
DEAL_CONTEXT_WORD_WARN = 1250  # soft notice: heads-up, still loads

# Company-norms context (this company's routine vendors/practices) — operator-
# authored, PERMANENT context (unlike DEAL_CONTEXT_MD's per-quarter notes
# above). Lives in data/input/, matching DEAL_PROFILE_DIR's operator-editable-
# context placement, not workspace/ (the source-workbook + per-quarter-notes
# folder). Read live by prompts.load_company_norms on every Phase-2 call;
# missing/empty file = no context (never fabricated).
COMPANY_NORMS_MD = DATA_IN / "companynorm.md"

# Classifier doctrine — single source of truth, read live on every run so an
# edit here needs no code change or re-sync (see prompts.load_baseline_instructions).
CLASSIFIER_DOCTRINE_MD = REPO_ROOT / "doctrines" / "classifier.md"

# Phase-1 sweep instructions — operator-amendable, read live each run (same
# pattern as CLASSIFIER_DOCTRINE_MD above).
DEALBUILDER_MD = REPO_ROOT / "doctrines" / "dealbuilder.md"

# Local invoice library: per-invoice PDFs live directly in externalinvoices/,
# named by invoice number (e.g. 10239.pdf). A row that mines key 10239 resolves
# to that file and reads it -- see invoice_mining.resolve_local. The CSV is an
# optional index alongside them (page ranges + entityid disambiguation); absent,
# resolution runs on filenames alone.
INVOICE_DIR = DATA_ROOT / "externalinvoices"
INVOICE_LOOKUP_CSV = DATA_ROOT / "externalinvoices" / "invoice_lookup.csv"

SHEET_NAME = "G&A MRI Records - With Link"  # default worksheet read/written across the pipeline

# Classification scope defaults (new dataset holds 12+ months; we classify a
# recent window and ignore immaterial amounts unless overridden via CLI flags).
SCOPE_MONTHS_DEFAULT = 3          # latest N distinct periods present in the file
SCOPE_MIN_USD_DEFAULT = 999.0     # operator's default; rows with |USD Amount| below this are out of scope

# Acquisition & Transaction (M&A) account — every row here is auto-labeled
# non_recurring by rule (basis "ma_account_rule") and filtered out of Phase-2
# classification; these rows are the sole input to the Phase-1 deal-profile
# sweep, which is live (real workbooks carry rows on this account most
# quarters). The pipeline's "no M&A rows" graceful-skip path only fires if a
# scope/quarter selection happens to exclude all of them.
MA_ACCTNUM = "MR58200000"

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
DEFAULT_MODEL = "claude-sonnet-4-6"
# ^ The FLOOR model: DEFAULT_MODEL remains the name every existing caller reads
# (cli default, probe-limits, reporting/console fallbacks). Two-tier policy: rows
# with a readable invoice run on INVOICE_MODEL (below), everything else on this
# floor. See model_for_batch.
#
# Two-tier model policy: invoice/vision batches get the stronger model,
# every other row runs on the floor (DEFAULT_MODEL). Swap INVOICE_MODEL to
# "claude-opus-4-8" later for a one-line accuracy dial-up (pricing rows below).
INVOICE_MODEL = "claude-sonnet-5"


def model_for_batch(batch, floor_model=None):
    """Return the model a batch should run on. A batch containing any item
    with a readable invoice (kind pdf/text) runs on INVOICE_MODEL; otherwise
    the floor (floor_model, or DEFAULT_MODEL). Pure — operates on WorkItem
    dicts, no imports, so both classify.py and deal_profile.py can call it
    without a cross-module dependency."""
    floor = floor_model or DEFAULT_MODEL
    for it in batch:
        inv = it.get("invoice") or {}
        if inv.get("kind") in ("pdf", "text"):
            return INVOICE_MODEL
    return floor


# ESTIMATE ONLY (forecast output estimate + limiter OTPM charge). The actual
# per-request output ceiling is the flat MAX_TOKENS_CLASSIFY_BATCH below —
# the old per-row formula (200 + 350*rows) invited truncation on verbose
# batches, and truncation triggered paid split-retry cascades. max_tokens
# costs nothing unless generated, so the request cap is deliberately generous
# while the terse output style (prompts.py + classifier.md) keeps actual
# generation far below it.
MAX_TOKENS_PER_ROW_OUTPUT = 350
MAX_TOKENS_CLASSIFY_BATCH = 8000      # flat per-request output ceiling (Phase 2)
MAX_TOKENS_DEAL_PROFILE = 8000
# SDK-level retry budget for 429 / 5xx / network — 2 retries = 3 attempts
# total (was 4). The pipeline has its own outer retry story (error records
# are excluded from resume, so a rerun re-decides them at $0 bookkeeping
# cost), which makes deep SDK retries redundant insurance; the billable
# waste case is a read-timeout the server actually completed, and each
# silent retry re-pays it.
API_MAX_RETRIES = 2
# Vision (PDF-as-image) requests get ZERO retries = exactly 1 attempt: a
# document that fails/times out once will almost always fail again, so a retry
# only makes the run wait a second full vision timeout on a single stuck row.
# Instead of retrying the image request, a failed vision call falls back ONCE
# to a text/row-only request (see the vision->text fallback in
# classify._process_batch / deal_profile._process_sweep_batch) — faster, on the
# floor model, using the row's own information when the invoice can't be read.
# Applied per-request via client.with_options().
VISION_MAX_RETRIES = 0
# The SDK's default read timeout is 600s, and a timeout is silently retried up
# to API_MAX_RETRIES times — one stuck PDF-vision request turned into a 40+
# minute silent hang with no operator-visible progress. 120s fails fast into
# classify.py's existing per-row error-record path instead (vision rows are
# batched alone, so the worst case this bounds is a single row, not a batch).
API_TIMEOUT_S = 120.0

# Per-request timeouts for Phase-1 sweep batches (mirrors the API_TIMEOUT_S
# rationale above, scoped to the sweep's own batch shape): text batches can
# hold several rows, so a slightly longer bound; vision batches are always a
# single row (classify.size_batches isolates them), so the model has to
# reason over one page image, not a batch's worth.
SWEEP_API_TIMEOUT_S = 90.0
SWEEP_API_TIMEOUT_VISION_S = 150.0

# Models that ACCEPT sampling params (temperature/top_p). This is an
# ALLOWLIST, not a denylist: only models we've confirmed take temperature are
# listed; everything else (all Claude 5 family, opus-4-x, and any unknown
# future model) omits it and fails safe. A denylist went stale every time a
# new model shipped — the claude-sonnet-5 400 ("temperature is deprecated for
# this model") slipped through precisely because it wasn't on the old denylist.
_SAMPLING_PARAM_MODELS = ("sonnet-4", "sonnet-3", "haiku-4", "haiku-3")


def supports_sampling_params(model: str) -> bool:
    """True when the model accepts temperature/top_p (so we can pin temperature=0).
    Unknown/unmatched models return False — never send temperature on a guess."""
    m = model.lower()
    return any(tag in m for tag in _SAMPLING_PARAM_MODELS)


# ---------------------------------------------------------------------------
# Token-budget batching constants
# ---------------------------------------------------------------------------
PER_ROW_OVERHEAD_TOKENS = 860         # packet + per-row instruction overhead
CHARS_PER_TOKEN = 4                   # rough text→token estimate
VISION_TOKENS_PER_PAGE = (3700, 5300) # PDF page as image — RANGE, few samples
VISION_TOKENS_PER_PAGE_MID = 4500
BATCH_TARGET_TOKENS_DEFAULT = 18_000  # clamped against measured ITPM
DEAL_PROFILE_TARGET_TOKENS = 20_000   # Phase-1 request size target; split if over
# Per-quarter token budget for the deal index block in the Phase-2 prompt.
# NOTE: the known-deal index is deliberately UNCAPPED — deal_profile_context_index
# emits every deal's full compact line. The whole system prompt is cached after
# batch 1, so a larger index costs cache-read pricing per batch, not full input
# pricing; dropping a deal (which the classifier then cannot recognize) is never
# worth saving those few tokens.

# ---------------------------------------------------------------------------
# Invoice reading
# ---------------------------------------------------------------------------
INVOICE_MAX_BYTES = 20 * 1024 * 1024  # oversize → error, never truncate
INVOICE_TIMEOUT_S = 10
INVOICE_RETRIES = 1                   # transport-level retries with backoff
PAGE_FULL_READ_MAX = 20   # read every page when the window is <= this many pages
PAGE_EDGE_COUNT    = 2    # when the window is longer, read only the first N + last N

# A raw invoice_url/mined-key reference with fewer than this many
# alphanumeric characters (e.g. "123", "a51") is noise, not a real pointer —
# see prep._is_substantive_reference. Never fetched/retried, never counted
# as a genuine invoice_read_failed.
MIN_SUBSTANTIVE_REFERENCE_CHARS = 4

# ---------------------------------------------------------------------------
# Microsoft Graph (OneDrive/SharePoint invoice links) — OPTIONAL. An
# invoice_url pointing at OneDrive/SharePoint can't be fetched anonymously
# (Microsoft returns a login page, not the file); connecting lets
# invoice_read fetch it via Graph on the operator's own delegated
# permissions instead (see graph_auth.py / graph_fetch.py). Entirely
# additive: with no token ever connected, these links behave exactly as
# before this feature existed (invoice_unavailable, same as any other
# unreachable URL) -- nothing here is required for a run.
#
# IDs are the app registration IT provided (not secrets -- a public client
# has no client secret; delegated auth is scoped to whatever the signed-in
# operator already has access to) -- but the tenant ID identifies this
# specific Microsoft 365 tenant, so it's still read from the environment
# (.env / GRAPH_TENANT_ID, GRAPH_CLIENT_ID) rather than hardcoded, the same
# way ANTHROPIC_API_KEY is. GRAPH_SCOPES assumes IT granted admin consent for
# Files.Read.All + Sites.Read.All; confirm with IT if the first interactive
# sign-in reports a scope/consent error.
# ---------------------------------------------------------------------------
GRAPH_TENANT_ID = os.environ.get("GRAPH_TENANT_ID", "").strip()
GRAPH_CLIENT_ID = os.environ.get("GRAPH_CLIENT_ID", "").strip()
GRAPH_AUTHORITY = f"https://login.microsoftonline.com/{GRAPH_TENANT_ID}" if GRAPH_TENANT_ID else ""
GRAPH_SCOPES = ["Files.Read.All", "Sites.Read.All"]


def graph_configured() -> bool:
    return bool(GRAPH_TENANT_ID and GRAPH_CLIENT_ID)
# Cached token (incl. refresh token) so a browser prompt is needed only once,
# not every run -- lives under DATA_ROOT alongside other external business
# data, never in the repo.
GRAPH_TOKEN_CACHE = DATA_ROOT / "graph_token_cache.bin"

# ---------------------------------------------------------------------------
# Scheduler / rate limits
# ---------------------------------------------------------------------------
MAX_WORKERS_UNMEASURED = 2            # Phase 2 refuses to exceed this without a probe

# Hard ceiling on concurrent in-flight batch requests, applied AFTER the
# adaptive derivation in classify.compute_max_workers. It bounds the Ctrl-C
# blast radius (only in-flight batches are unrecoverable spend once
# interrupted) and burst-429 risk from token-estimate error — NOT the API
# tier, which allows far more. Tier headroom is 10k RPM / 10M ITPM / 2M OTPM
# at 80% — the ceiling, not the tier, is the limiter here; the Ctrl-C blast
# radius at this ceiling is ~one wave of solo-invoice batches ≈ cents.
# HARDCODED FOR NOW and subject to change: raise it after watching real runs.
MAX_WORKERS_CEILING = 30
# Assumed per-batch wall latency (seconds), used to derive worker count and
# the wall-clock forecast. Calibrate against the actual wall clock the run
# summary now prints.
SCHED_CLASSIFY_LATENCY_S = 45.0
SCHED_SWEEP_LATENCY_S = 75.0

# Runtime spend rail: each paid phase aborts (salvaging everything already
# durable, same path as Ctrl-C) once its actual accumulated cost exceeds
# this multiple of ITS OWN pre-run forecast's HIGH end. Anchored on the high
# end, not the midpoint, because actuals legitimately drift high when
# invoices skew vision-heavy; runs usually land UNDER the forecast, so a
# healthy run never hears from this. Checked per completed batch, so with N
# workers in flight the overshoot past the cap is bounded by ~one wave of
# batches. Deliberately LOOSE: this is a runaway-spend backstop, not a tight
# budget gate — it should fire only on a SEVERE overrun (2x the forecast),
# never trip a run that merely lands a bit over estimate.
SPEND_CAP_MULTIPLIER = 2.0

# Pricing (USD per 1M tokens) for the pre-run forecast. Ranges, not points —
# verify against current published pricing when the forecast starts to drift
# from actuals (the Run Summary prints the delta).
PRICING_PER_MTOK = {
    "claude-opus-4-8":   {"input": 5.00, "output": 25.00},
    "claude-opus-4-7":   {"input": 5.00, "output": 25.00},
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00},
    "claude-sonnet-5":   {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5":  {"input": 1.00, "output": 5.00},
}
DEFAULT_PRICING = {"input": 3.00, "output": 15.00}


def pricing_for(model: str) -> dict[str, float]:
    for key, p in PRICING_PER_MTOK.items():
        if key in model:
            return p
    return DEFAULT_PRICING


def load_rate_limits() -> dict | None:
    """Read rules/rate_limits.json (written by `probe-limits`). None if absent
    or unreadable — callers must then enforce MAX_WORKERS_UNMEASURED."""
    try:
        with open(RATE_LIMITS_JSON, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def batch_target_tokens(rate_limits: dict | None) -> int:
    """Clamp the batch token target to a safe fraction of measured input-TPM:
    if measured ITPM is low, target = min(default, itpm * 0.5)."""
    if not rate_limits:
        return BATCH_TARGET_TOKENS_DEFAULT
    itpm = rate_limits.get("input_tokens_limit")
    if isinstance(itpm, (int, float)) and itpm > 0:
        return int(min(BATCH_TARGET_TOKENS_DEFAULT, itpm * 0.5))
    return BATCH_TARGET_TOKENS_DEFAULT


def api_key_present() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


class WorkbookDiscoveryError(Exception):
    """workspace/ holds zero or multiple .xlsb files -- cli.main catches this
    and prints it as an operator-facing ERROR (never a stack trace), same
    pattern as deal_profile.CorruptProfileError."""


def discover_workbook(workspace_dir: Path = WORKSPACE_DIR) -> Path:
    """Find the single source workbook an operator dropped in workspace/.

    Accepts both .xlsb and .xlsx (Q2's flattened output is .xlsx; the older
    accounting extract is .xlsb) -- still enforces exactly one workbook total
    across both extensions.

    Only called for commands that actually need a workbook AND were not
    given an explicit --workbook (cli.py resolves lazily, at the top of each
    such command, never eagerly at argparse-parse time) -- `recover` never
    calls this, so it keeps working against an empty workspace/."""
    matches = sorted(workspace_dir.glob("*.xlsb")) + sorted(workspace_dir.glob("*.xlsx"))
    if not matches:
        raise WorkbookDiscoveryError(
            f"No workbook (.xlsb or .xlsx) found in {workspace_dir}\\ -- drop "
            f"your G&A records workbook there and rerun"
        )
    if len(matches) > 1:
        names = ", ".join(p.name for p in matches)
        raise WorkbookDiscoveryError(
            f"Found {len(matches)} workbook files in {workspace_dir}\\ -- keep "
            f"exactly one ({names})"
        )
    return matches[0]
