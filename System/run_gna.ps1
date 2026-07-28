# run_gna.ps1 -- launch wrapper for gna_pipeline.
#
# Discovers a Python 3.12+ interpreter (py launcher, PATH, or common install
# dirs), installs dependencies on first run if needed, loads ANTHROPIC_API_KEY
# (and PATH) from the local .env file, then either:
#   * forwards all arguments verbatim to `python -m gna_pipeline` (hard mode), or
#   * if no arguments are given, presents a FINANCE menu built around what an
#     operator actually wants to do -- run the pipeline and read the results --
#     with the power-user commands tucked one level down in "Advanced".
#
# Hard mode (unchanged -- scriptable, exactly as before):
#   .\run_gna.ps1 ingest-check
#   .\run_gna.ps1 quarters
#   .\run_gna.ps1 probe-limits
#   .\run_gna.ps1 deal-profile
#   .\run_gna.ps1 deal-profile --quarters 2026Q1
#   .\run_gna.ps1 run --n 15 --dry-run
#   .\run_gna.ps1 run --quarter 2026Q1 --yes
#   .\run_gna.ps1 run --guided
#   .\run_gna.ps1 run --quarters 2025Q4,2026Q1 --yes
#   .\run_gna.ps1 run --months 6 --min-usd 1000 --yes
#   .\run_gna.ps1 run --months all --min-usd 0 --yes    # the WHOLE workbook
#   .\run_gna.ps1 run --yes
#   .\run_gna.ps1 recover
#
# Scope, in one place: a bare `run` classifies the latest 3 distinct months in
# the file with |USD Amount| >= 999 (config.SCOPE_MONTHS_DEFAULT / _MIN_USD_
# DEFAULT) -- this is "recent activity", the everyday run. `--months all
# --min-usd 0` is the ONLY thing that classifies the entire workbook; the menu
# surfaces that explicitly as "Full workbook run" so it is never confused with
# the recent-activity run. `--months` scopes which non-M&A periods reach
# Phase-2 classification; `--quarters` scopes only the M&A deal-profile sweep;
# `--quarter`/`--guided` derive BOTH from one quarter label; `--rows` bypasses
# both scope filters entirely.
#
# Interactive mode:
#   .\run_gna.ps1            # <- FINANCE menu (below)

$ErrorActionPreference = "Stop"

# This script lives in System/ (one level below the repo root). Run from the
# repo root -- its PARENT -- so 'gna_pipeline' imports resolve and relative
# paths hold, exactly as when this script sat at the root. (.env and
# requirements.txt moved into System/ alongside this script, so those are still
# read from $PSScriptRoot below.)
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

# --- Load .env (KEY=VALUE per line; '#' comments and blank lines ignored) --------
$envPath = Join-Path $PSScriptRoot ".env"
if (Test-Path $envPath) {
    foreach ($line in Get-Content $envPath) {
        $trimmed = $line.Trim()
        if ($trimmed -eq "" -or $trimmed.StartsWith("#")) { continue }
        $idx = $trimmed.IndexOf("=")
        if ($idx -lt 1) { continue }
        $key = $trimmed.Substring(0, $idx).Trim()
        $val = $trimmed.Substring($idx + 1).Trim().Trim('"').Trim("'")
        if ($key -eq "PATH") {
            # Prepend, never clobber, the existing PATH.
            $env:PATH = $val + ";" + $env:PATH
        } else {
            Set-Item -Path ("Env:" + $key) -Value $val
        }
    }
} else {
    Write-Warning ".env not found at: $envPath  (relying on existing environment)"
}

# --- Discover a Python 3.12+ interpreter -----------------------------------------
# Tries, in order: the 'py' launcher pinned to 3.12, the 'py' launcher's
# default (in case only a newer 3.x is registered), 'python'/'python3' on
# PATH, then common per-user/system install directories. Whichever candidate
# is accepted, $python is resolved to that interpreter's actual python.exe
# path (via sys.executable) so every later '& $python ...' call below is a
# plain single-executable invocation, unchanged from before.
$minPythonVersion = [version] "3.12.0"

function Get-CandidateVersion {
    param([string] $Exe, [string[]] $PreArgs = @())
    try {
        $out = & $Exe @PreArgs -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
        return [version] ($out | Select-Object -Last 1).Trim()
    } catch {
        return $null
    }
}

function Get-CandidateExecutable {
    param([string] $Exe, [string[]] $PreArgs = @())
    try {
        $out = & $Exe @PreArgs -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
        return ($out | Select-Object -Last 1).Trim()
    } catch {
        return $null
    }
}

$python = $null

# 1) Windows launcher, explicit 3.12; then its bare default (covers a
#    newer-only install where 3.12 itself isn't registered).
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($launcherArgs in @(@("-3.12"), @())) {
        if ($python) { break }
        $ver = Get-CandidateVersion -Exe "py" -PreArgs $launcherArgs
        if ($ver -and $ver -ge $minPythonVersion) {
            $python = Get-CandidateExecutable -Exe "py" -PreArgs $launcherArgs
        }
    }
}

# 2) 'python' / 'python3' already on PATH, if >= 3.12.
if (-not $python) {
    foreach ($candidate in @("python", "python3")) {
        if ($python) { break }
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            $ver = Get-CandidateVersion -Exe $candidate
            if ($ver -and $ver -ge $minPythonVersion) {
                $python = Get-CandidateExecutable -Exe $candidate
            }
        }
    }
}

# 3) Common install directories (newest-looking first; version-checked below
#    regardless of order).
if (-not $python) {
    $searchRoots = @(
        "$env:LOCALAPPDATA\Programs\Python",
        "$env:ProgramFiles",
        "${env:ProgramFiles(x86)}",
        "C:\"
    )
    $candidates = foreach ($root in $searchRoots) {
        if ($root -and (Test-Path $root)) {
            Get-ChildItem -Path $root -Directory -Filter "Python3*" -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                ForEach-Object { Join-Path $_.FullName "python.exe" }
        }
    }
    foreach ($candidate in $candidates) {
        if ($python) { break }
        if (Test-Path $candidate) {
            $ver = Get-CandidateVersion -Exe $candidate
            if ($ver -and $ver -ge $minPythonVersion) { $python = $candidate }
        }
    }
}

if (-not $python) {
    Write-Error ("No Python 3.12+ interpreter found. Tried the 'py' launcher, " + `
        "'python'/'python3' on PATH, and common install directories under " + `
        "%LOCALAPPDATA%, %ProgramFiles%, and C:\. Install Python 3.12 or later " + `
        "from https://www.python.org/downloads/ and re-run.")
    exit 1
}
$env:PATH = (Split-Path $python) + ";" + $env:PATH

# --- First-run dependency self-install -------------------------------------------
# Idempotent: only touches pip if the runtime deps aren't already installed. Runs
# on EVERY launch (not just the first), so this checks presence via
# importlib.util.find_spec rather than actually importing the packages --
# `import anthropic` alone eagerly loads its Vertex/Bedrock/tools/beta submodules
# and costs ~1-2s every time; find_spec confirms it's installed without running
# any of that module code.
$depsOk = $false
try {
    & $python -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(m) for m in ('anthropic', 'openpyxl', 'pyxlsb', 'pypdf', 'msal')) else 1)" 2>$null
    $depsOk = ($LASTEXITCODE -eq 0)
} catch {
    $depsOk = $false
}
if (-not $depsOk) {
    $reqPath = Join-Path $PSScriptRoot "requirements.txt"
    if (-not (Test-Path $reqPath)) {
        Write-Error "requirements.txt not found at: $reqPath"
        exit 1
    }
    Write-Host "First run: installing dependencies from requirements.txt..." -ForegroundColor DarkGray
    & $python -m pip install -q -r $reqPath
    if ($LASTEXITCODE -ne 0) {
        Write-Error "pip install failed. Try it manually: `"$python`" -m pip install -r `"$reqPath`""
        exit 1
    }
}

# Force Python's stdout/stderr to be unbuffered regardless of how this script is
# invoked. Python line-buffers when stdout is a real console but falls back to
# full block-buffering (~8KB chunks) when it isn't -- some terminals/launchers
# hand a child process a non-tty stdout even in an interactive session, which
# silently held back print() calls that don't pass flush=True until the buffer
# filled or the process exited (looked like "the script prints nothing").
$env:PYTHONUNBUFFERED = "1"

# ================================================================================
# HARD MODE: any arguments -> forward verbatim, exactly like the original wrapper.
# ================================================================================
if ($args.Count -gt 0) {
    & $python -u -m gna_pipeline @args
    exit $LASTEXITCODE
}

# ================================================================================
# INTERACTIVE MODE: no arguments -> FINANCE menu.
# ================================================================================

# --- Paths the launcher itself reasons about (results + resume stores). Kept in
#     one place so the auto-rebuild check below and the "open results" helper
#     never drift from config.py. -------------------------------------------------
$ResultsXlsx    = Join-Path $PSScriptRoot "workspace\results\classified.xlsx"
$ResultsFolder  = Join-Path $PSScriptRoot "workspace\results"
$ClassifyJsonl  = Join-Path $PSScriptRoot "data\output\results\results.jsonl"
$DealJsonl      = Join-Path $PSScriptRoot "data\input\dealprofile\results.jsonl"

$apiKeySet = [bool] $env:ANTHROPIC_API_KEY
$keyBadge  = if ($apiKeySet) { "set" } else { "MISSING" }

# --- Small input helpers ---------------------------------------------------------

function Read-NonEmpty {
    param([string] $Prompt, [string] $Default = $null)
    while ($true) {
        $suffix = if ($Default) { " [$Default]" } else { "" }
        $v = (Read-Host ($Prompt + $suffix)).Trim()
        if ($v -eq "" -and $Default) { return $Default }
        if ($v -ne "") { return $v }
        Write-Host "  (a value is required)" -ForegroundColor Yellow
    }
}

function Confirm-Yes {
    param([string] $Prompt)
    return ((Read-Host ($Prompt + " [y/N]")).Trim().ToLower() -in @("y", "yes"))
}

function Read-Optional {
    param([string] $Prompt)
    return (Read-Host $Prompt).Trim()
}

function Pause-Menu {
    [void] (Read-Host "`nPress Enter to return to the menu")
}

# Optional --months / --min-usd, prompted with plain-English wording and
# returned as a (possibly empty) argument array. Factored out so every command
# that accepts a classification scope asks for it identically (the old menu
# re-inlined slightly different wording in five different places).
function Get-ScopeArgs {
    $extra = @()
    $months = Read-Optional "  Months to include (blank = latest 3; 'all' = every month; or e.g. 202601,202602)"
    if ($months -ne "") { $extra += @("--months", $months) }
    $minUsd = Read-Optional "  Minimum |USD| per row (blank = 999; 0 = include everything)"
    if ($minUsd -ne "") { $extra += @("--min-usd", $minUsd) }
    return $extra
}

# --- The one place that actually invokes the pipeline ----------------------------
# -Quiet suppresses the "> python -m gna_pipeline ..." echo: shown in Advanced
# (where seeing the underlying command is useful) and hidden for the everyday
# finance runs (where it is just noise).
#
# CRITICAL: this function must NOT `return`/emit anything, and callers must NOT
# assign its result (`$x = Invoke-Gna ...`). Assigning the result of a function
# whose body runs a native command CAPTURES that command's stdout into the
# variable instead of letting it reach the console -- which would swallow the
# pipeline's cost forecast, per-row narration, and (fatally) its interactive
# `Pick a quarter` / `Proceed? [y/N]` prompts, leaving the window hung on an
# invisible prompt. Callers read the exit code from the automatic $LASTEXITCODE
# after the call instead (set by the `& $python` line below, which is the last
# native command Invoke-Gna runs).
function Invoke-Gna {
    param([string[]] $GnaArgs, [switch] $Quiet)
    Write-Host ""
    if (-not $Quiet) {
        Write-Host ("> python -m gna_pipeline " + ($GnaArgs -join " ")) -ForegroundColor DarkCyan
        Write-Host ""
    }
    & $python -u -m gna_pipeline @GnaArgs
}

# --- Results hand-off ------------------------------------------------------------
# Every run finishes with classified.xlsx already written by the pipeline
# (pipeline._stage11_output, even on an interrupted/declined run); point the
# operator straight at it and open the folder instead of leaving them to hunt.
function Open-Results {
    Write-Host ""
    if (Test-Path $ResultsXlsx) {
        Write-Host ("Results: " + $ResultsXlsx) -ForegroundColor Cyan
        try { Start-Process $ResultsFolder } catch { }
    } else {
        Write-Host "Results: classified.xlsx not written yet." -ForegroundColor DarkGray
    }
}

# --- Automatic rebuild -----------------------------------------------------------
# The pipeline writes classified.xlsx at the end of every run, so normally
# there is nothing to do. This covers the one case where that write couldn't
# happen -- the file was open in Excel (locked) during the run, or was deleted:
# on the NEXT launch, if saved results exist but classified.xlsx is missing or
# older than the newest results.jsonl, rebuild it via `recover` at zero API
# cost. Best-effort and quiet: if it can't (e.g. no workbook in workspace yet),
# it stays out of the way and the menu still works.
function Sync-Results {
    $newestJsonl = [datetime]::MinValue
    $haveResults = $false
    foreach ($j in @($ClassifyJsonl, $DealJsonl)) {
        if ((Test-Path $j) -and ((Get-Item $j).Length -gt 0)) {
            $haveResults = $true
            $t = (Get-Item $j).LastWriteTime
            if ($t -gt $newestJsonl) { $newestJsonl = $t }
        }
    }
    if (-not $haveResults) { return }

    $needsRebuild = -not (Test-Path $ResultsXlsx)
    if (-not $needsRebuild) {
        if ((Get-Item $ResultsXlsx).LastWriteTime -lt $newestJsonl) { $needsRebuild = $true }
    }
    if (-not $needsRebuild) { return }

    Write-Host "Saved results found without an up-to-date classified.xlsx -- rebuilding it (no API cost)..." -ForegroundColor DarkGray
    # Best-effort and silent. Relax ErrorActionPreference locally: this script
    # runs under "Stop", and merging a native command's stderr can surface as a
    # terminating error in Windows PowerShell 5.1 even on a clean exit. We only
    # care about the exit code and whether the file appeared.
    $rc = 1
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $python -u -m gna_pipeline recover 2>&1 | Out-Null
        $rc = $LASTEXITCODE
    } catch {
        $rc = 1
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    if ($rc -eq 0 -and (Test-Path $ResultsXlsx)) {
        Write-Host "  rebuilt workspace\results\classified.xlsx" -ForegroundColor DarkGray
    } else {
        Write-Host "  (could not rebuild automatically -- use Advanced -> Rebuild results Excel if you need it)" -ForegroundColor DarkGray
    }
}

# --- Finance run wrapper: run, report, open results ------------------------------
function Invoke-FinanceRun {
    param([string[]] $GnaArgs)
    if (-not $apiKeySet) {
        Write-Host "  ANTHROPIC_API_KEY is not set -- this run will fail. Add it to .env first (see QUICKSTART.md)." -ForegroundColor Yellow
    }
    Invoke-Gna -GnaArgs $GnaArgs -Quiet
    $code = $LASTEXITCODE
    Write-Host ""
    Write-Host ("[finished -- exit code $code]") -ForegroundColor $(if ($code -eq 0) { "DarkGray" } else { "Red" })
    Open-Results
}

# --- Q2 two-file run: the everyday Q2 path ---------------------------------------
# Two Excel files in (A&T + G&A), one quarter chosen, Human Review (and every
# other tab) out. Delegates entirely to `run-q2`, which flattens the two
# workbooks into one sheet, then reuses `run --guided` -- so the operator sees
# the quarter list and picks one, and that single quarter drives BOTH the deal
# profile and the classification. No scope prompts here: the quarter pick IS
# the scope.
function Invoke-Q2Run {
    Write-Host ""
    Write-Host "Q2 run: point me at your two Excel files." -ForegroundColor Cyan
    Write-Host "  (paste the full path to each -- surrounding quotes are fine)" -ForegroundColor DarkGray
    $ga = (Read-NonEmpty "  G&A workbook (the multi-tab one)").Trim('"')
    $at = (Read-NonEmpty "  A&T workbook (Acquisition & Transaction)").Trim('"')
    if (-not (Test-Path -LiteralPath $ga)) { Write-Host "  G&A file not found: $ga" -ForegroundColor Yellow; return }
    if (-not (Test-Path -LiteralPath $at)) { Write-Host "  A&T file not found: $at" -ForegroundColor Yellow; return }
    Invoke-FinanceRun -GnaArgs @("run-q2", "--ga", $ga, "--at", $at, "--guided")
}

# ================================================================================
# Advanced submenu -- diagnostics, the true full-workbook run, and the
# targeted/sample runs a finance operator rarely needs day-to-day.
# ================================================================================
function Invoke-AdvancedMenu {
    while ($true) {
        Write-Host ""
        Write-Host "---------------------------------------------------------------------" -ForegroundColor DarkCyan
        Write-Host " Advanced" -ForegroundColor DarkGray
        Write-Host "---------------------------------------------------------------------" -ForegroundColor DarkCyan
        Write-Host ""
        Write-Host " FREE / local (no API cost)" -ForegroundColor DarkCyan
        Write-Host "   1) Preview cost (dry-run)   Do the free work + print the cost forecast, then STOP"
        Write-Host "   2) Rebuild results Excel    Rebuild classified.xlsx from saved results (`$0)"
        Write-Host "   3) List quarters            Show quarters in the workbook (row / M&A counts)"
        Write-Host "   4) Ingest check             Read the workbook; print row / warning / invoice stats"
        Write-Host ""
        Write-Host " PAID (calls the Anthropic API)"
        Write-Host "   5) Full workbook run        EVERY period, EVERY row -- no 3-month / `$999 limit"
        Write-Host "   6) Sample run (N rows)      Cheap end-to-end rehearsal on N rows"
        Write-Host "   7) Classify specific rows   Re-run only the Excel row numbers you name"
        Write-Host "   8) Build deal profile only  Phase 1 standalone (the M&A deal sweep)"
        Write-Host "   9) Probe API limits         Measure rate limits (1-token ping)"
        Write-Host ""
        Write-Host "   b) back"
        Write-Host ""

        $choice = (Read-Host "Select").Trim().ToLower()
        if ($choice -in @("b", "back", "q", "quit", "exit", "")) { return }

        $cmd = $null
        switch ($choice) {
            "1" {
                $cmd = @("run", "--dry-run")
                $cmd += @(Get-ScopeArgs)
            }
            "2" { $cmd = @("recover") }
            "3" { $cmd = @("quarters") }
            "4" {
                $cmd = @("ingest-check")
                $cmd += @(Get-ScopeArgs)
            }
            "5" {
                if (-not $apiKeySet) { Write-Host "ANTHROPIC_API_KEY is not set; this will fail." -ForegroundColor Yellow }
                # The ONLY run that covers the entire workbook: every month, no
                # dollar floor. Wired in for the operator so "full workbook"
                # actually means full workbook -- no need to know 'all' / '0'.
                Write-Host "  This classifies the ENTIRE workbook (all periods, all rows)." -ForegroundColor Magenta
                Write-Host "  Python prints its cost forecast and asks to proceed before spending anything." -ForegroundColor Magenta
                $cmd = @("run", "--months", "all", "--min-usd", "0")
                if (Confirm-Yes "  Skip the in-pipeline confirm prompt (--yes)?") { $cmd += "--yes" }
            }
            "6" {
                if (-not $apiKeySet) { Write-Host "ANTHROPIC_API_KEY is not set; this will fail." -ForegroundColor Yellow }
                $n = Read-NonEmpty "  Sample size N (e.g. 15)"
                $cmd = @("run", "--n", $n)
                $cmd += @(Get-ScopeArgs)
                if (Confirm-Yes "  Add --dry-run (forecast only, no paid call)?") { $cmd += "--dry-run" }
                elseif (Confirm-Yes "  Skip the in-pipeline confirm prompt (--yes)?") { $cmd += "--yes" }
            }
            "7" {
                if (-not $apiKeySet) { Write-Host "ANTHROPIC_API_KEY is not set; this will fail." -ForegroundColor Yellow }
                $r = Read-NonEmpty "  Excel row(s), comma-separated (e.g. 63 or 63,64)"
                $cmd = @("run", "--rows", $r)
                if (Confirm-Yes "  Skip the in-pipeline confirm prompt (--yes)?") { $cmd += "--yes" }
            }
            "8" {
                if (-not $apiKeySet) { Write-Host "ANTHROPIC_API_KEY is not set; this will fail." -ForegroundColor Yellow }
                $cmd = @("deal-profile")
                $q = Read-Optional "  Quarter(s) to sweep -- e.g. 2026Q1, 2025Q4,2026Q1, or a count like 2 (blank = latest quarter present)"
                if ($q -ne "") { $cmd += @("--quarters", $q) }
                $cmd += @(Get-ScopeArgs)
                $m = (Read-Host "  Override model? (blank = default)").Trim()
                if ($m -ne "") { $cmd += @("--model", $m) }
                if (Confirm-Yes "  Add --dry-run (forecast only, no paid call)?") { $cmd += "--dry-run" }
                elseif (Confirm-Yes "  Skip the in-pipeline confirm prompt (--yes)?") { $cmd += "--yes" }
            }
            "9" {
                if (-not $apiKeySet) { Write-Host "ANTHROPIC_API_KEY is not set; this will fail." -ForegroundColor Yellow }
                $cmd = @("probe-limits")
            }
            default {
                Write-Host "Unrecognized choice: '$choice'" -ForegroundColor Yellow
            }
        }

        if ($null -ne $cmd) {
            Invoke-Gna -GnaArgs $cmd
            $code = $LASTEXITCODE
            Write-Host ""
            Write-Host ("[exit code $code]") -ForegroundColor $(if ($code -eq 0) { "DarkGray" } else { "Red" })
            # Anything that (re)writes classified.xlsx -- a run or a recover --
            # ends by pointing the operator at the results, same as the finance
            # runs do.
            if ($cmd[0] -in @("run", "recover")) { Open-Results }
            Pause-Menu
        }
    }
}

# ================================================================================
# FINANCE menu -- the everyday path. Two runs (recent activity, or one chosen
# quarter), each with Python's own cost forecast as the single money gate, and
# everything else one level down in Advanced.
# ================================================================================

# Make sure the operator always opens to an up-to-date classified.xlsx, even if
# a prior run's write was blocked by the file being open in Excel.
Sync-Results

:financeMenu while ($true) {
    Write-Host ""
    Write-Host "=====================================================================" -ForegroundColor Cyan
    Write-Host " G&A Non-Recurring pipeline" -ForegroundColor Cyan
    Write-Host (" ANTHROPIC_API_KEY: {0}" -f $keyBadge) -ForegroundColor $(if ($apiKeySet) { "DarkGray" } else { "Yellow" })
    Write-Host "=====================================================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "   1) Q2 run (A&T + G&A)      Pick your two Excel files, choose a quarter, classify it"
    Write-Host "   2) Run recent activity     Classify the latest 3 months of a loaded workbook (|USD| >= `$999)"
    Write-Host "   3) Run a specific quarter  Pick one quarter from a loaded workbook"
    Write-Host "   4) Advanced...             Preview cost, full-workbook run, rebuild, diagnostics"
    Write-Host ""
    Write-Host "   q) quit"
    Write-Host ""
    Write-Host "   Every run shows a cost forecast and asks you to confirm before spending." -ForegroundColor DarkGray
    Write-Host ""

    $choice = (Read-Host "Select").Trim().ToLower()
    if ($choice -in @("q", "quit", "exit", "")) { break }

    switch ($choice) {
        "1" {
            # The Q2 two-file path: flatten A&T + G&A, then guided quarter pick.
            Invoke-Q2Run
            Pause-Menu
        }
        "2" {
            # Bare `run` == recent-activity scope (latest 3 months, |USD| >= 999)
            # against the single workbook already in workspace/.
            Invoke-FinanceRun -GnaArgs @("run")
            Pause-Menu
        }
        "3" {
            # --guided lists the workbook's quarters, prompts for one, and runs
            # exactly that quarter with a freshly-built deal profile.
            Invoke-FinanceRun -GnaArgs @("run", "--guided")
            Pause-Menu
        }
        "4" { Invoke-AdvancedMenu }
        default {
            Write-Host "Unrecognized choice: '$choice'" -ForegroundColor Yellow
        }
    }
}
