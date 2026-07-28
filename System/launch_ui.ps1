# launch_ui.ps1 -- start the local G&A Classifier web UI (gna_server + frontend/).
#
# Discovers a Python 3.12+ interpreter (same logic as run_gna.ps1), installs
# the `ui` extras on first run if needed (fastapi/uvicorn/python-dotenv/
# python-multipart -- pyproject.toml's [ui] group), then starts the server on
# loopback and opens the browser. No Node/npm/build step -- the frontend is a
# no-build vanilla app served as static files by the server itself.
#
# Usage:
#   .\launch_ui.ps1

$ErrorActionPreference = "Stop"

# This script lives in System/ (one level below the repo root). Run from the
# repo root -- its PARENT -- so 'gna_pipeline'/'gna_server' imports resolve,
# `pip install -e .[ui]` finds pyproject.toml, and relative paths (workspace/,
# data/) hold, exactly as when this script sat at the root.
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -Path $repoRoot

# --- Load .env (KEY=VALUE per line; '#' comments and blank lines ignored) --------
# .env lives beside this script in System/, so read it from $PSScriptRoot.
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
# Same discovery order as run_gna.ps1 (kept self-contained here rather than
# shared, so either script runs standalone with nothing to source first):
# the 'py' launcher pinned to 3.12, its bare default, 'python'/'python3' on
# PATH, then common per-user/system install directories.
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

if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($launcherArgs in @(@("-3.12"), @())) {
        if ($python) { break }
        $ver = Get-CandidateVersion -Exe "py" -PreArgs $launcherArgs
        if ($ver -and $ver -ge $minPythonVersion) {
            $python = Get-CandidateExecutable -Exe "py" -PreArgs $launcherArgs
        }
    }
}

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

# --- First-run credential setup: helpers -----------------------------------------
# A missing ANTHROPIC_API_KEY is our "first run" signal: a fresh clone has no
# System\.env at all, and the app can't make a paid call without the key. When
# it's missing we ask for it (and, in the SAME one-time pass, the optional
# Microsoft Graph IDs), persist whatever is pasted to System\.env so later
# launches skip all of this, and -- once deps are installed, below -- verify the
# key with a 1-token probe. When the key is ALREADY set we touch none of this:
# no prompt, no probe. ($envPath was set above where .env is loaded.)

function Test-ValuePresent {
    param([string] $Value)
    return -not [string]::IsNullOrWhiteSpace($Value)
}

function Test-ApiKeyPresent {
    # Present == a non-blank value that isn't the .env.example placeholder
    # (sk-ant-api03-...), so a half-filled copy of the template still counts as
    # "needs setup" rather than silently sailing through with a fake key.
    $v = $env:ANTHROPIC_API_KEY
    if (-not (Test-ValuePresent $v)) { return $false }
    if ($v.Trim().EndsWith("...")) { return $false }
    return $true
}

function Read-RequiredValue {
    param([string] $Prompt)
    while ($true) {
        $v = (Read-Host $Prompt).Trim()
        if ($v -ne "") { return $v }
        Write-Host "  (a value is required -- paste it, or press Ctrl+C to quit)" -ForegroundColor Yellow
    }
}

function Read-OptionalValue {
    param([string] $Prompt)
    return (Read-Host $Prompt).Trim()
}

function Set-EnvValue {
    # Set KEY in THIS session (so the server we start below sees it -- the
    # server's own load_dotenv never overrides an already-set env var) AND
    # persist it to System\.env: update the line in place if the key already
    # exists (even blank/placeholder), else append. Every other line -- comments,
    # PATH, the other keys -- is preserved untouched. Written UTF-8 without a BOM
    # so python-dotenv reads the first key cleanly on a future launch.
    param([string] $Key, [string] $Value)
    Set-Item -Path ("Env:" + $Key) -Value $Value

    $line = "$Key=$Value"
    $pattern = "^" + [regex]::Escape($Key) + "\s*="
    if (Test-Path $envPath) {
        $found = $false
        $lines = @(Get-Content $envPath | ForEach-Object {
            $t = $_.Trim()
            if (-not $t.StartsWith("#") -and $t -match $pattern) {
                $found = $true
                $line
            } else {
                $_
            }
        })
        if (-not $found) { $lines += $line }
    } else {
        $lines = @($line)
    }
    $enc = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllLines($envPath, [string[]] $lines, $enc)
}

# --- First-run credential setup: prompt ------------------------------------------
$keyWasEntered = $false
if (-not (Test-ApiKeyPresent)) {
    Write-Host ""
    Write-Host "First-time setup" -ForegroundColor Cyan
    Write-Host "-----------------------------------------------------------------" -ForegroundColor Cyan
    Write-Host "No Anthropic API key found -- paste yours to finish setup." -ForegroundColor Yellow
    Write-Host "  (from https://console.anthropic.com/ -- it starts with 'sk-ant-')" -ForegroundColor DarkGray
    Write-Host ""
    $key = Read-RequiredValue "  Anthropic API key"
    Set-EnvValue -Key "ANTHROPIC_API_KEY" -Value $key
    $keyWasEntered = $true

    # Optional Microsoft Graph (OneDrive/SharePoint invoice links). Ask ONLY for
    # whichever ID is missing -- never re-ask for one that's already set. Both are
    # needed together, so a lone ID stays dormant until its partner is set; the
    # operator can skip both (Enter) and connect later from the app's OneDrive
    # button. This is the ONLY place Graph is prompted for -- it rides along with
    # first-run key setup and is never asked for again once a key exists.
    if (-not (Test-ValuePresent $env:GRAPH_TENANT_ID) -or -not (Test-ValuePresent $env:GRAPH_CLIENT_ID)) {
        Write-Host ""
        Write-Host "  Optional: Microsoft Graph access for OneDrive/SharePoint invoice links." -ForegroundColor DarkGray
        Write-Host "  Press Enter to skip either one -- you can connect later from the app." -ForegroundColor DarkGray
        Write-Host "  (Get these from IT's app registration.)" -ForegroundColor DarkGray
        if (-not (Test-ValuePresent $env:GRAPH_TENANT_ID)) {
            $tenant = Read-OptionalValue "  Graph tenant ID (blank = skip)"
            if ($tenant -ne "") { Set-EnvValue -Key "GRAPH_TENANT_ID" -Value $tenant }
        }
        if (-not (Test-ValuePresent $env:GRAPH_CLIENT_ID)) {
            $client = Read-OptionalValue "  Graph client ID (blank = skip)"
            if ($client -ne "") { Set-EnvValue -Key "GRAPH_CLIENT_ID" -Value $client }
        }
    }
    Write-Host ""
}

# --- First-run dependency self-install (base deps + the `ui` extras) ------------
# Checked on every launch via find_spec (cheap, no module import), not just
# once -- mirrors run_gna.ps1's rationale. Installs via `pip install -e .[ui]`
# (not requirements.txt, which only lists the core pipeline deps) so this is
# the one place fastapi/uvicorn/python-dotenv/python-multipart get pulled in.
$depsOk = $false
try {
    & $python -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(m) for m in ('anthropic', 'openpyxl', 'pyxlsb', 'pypdf', 'msal', 'fastapi', 'uvicorn', 'dotenv', 'multipart')) else 1)" 2>$null
    $depsOk = ($LASTEXITCODE -eq 0)
} catch {
    $depsOk = $false
}
if (-not $depsOk) {
    Write-Host "First run: installing dependencies (pip install -e .[ui])..." -ForegroundColor DarkGray
    & $python -m pip install -q -e ".[ui]"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "pip install failed. Try it manually: `"$python`" -m pip install -e `".[ui]`""
        exit 1
    }
}

# --- Verify a freshly-entered API key --------------------------------------------
# ONLY when the key was just entered above (never when it was already set). A
# 1-token probe (python -m gna_pipeline probe-limits) confirms the key is valid
# AND can reach Anthropic before the app starts. On failure we say why and let
# the operator paste a different key or start anyway (paid runs then fail until
# the key in System\.env is fixed). ErrorActionPreference is relaxed around the
# native call so a probe that exits non-zero is read from its exit code, not
# surfaced as a terminating error (Windows PowerShell 5.1 quirk).
if ($keyWasEntered) {
    while ($true) {
        Write-Host ""
        Write-Host "Checking your API key (1-token probe, costs about `$0)..." -ForegroundColor DarkGray
        $rc = 1
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & $python -u -m gna_pipeline probe-limits
            $rc = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $prevEAP
        }
        if ($rc -eq 0) {
            Write-Host "API key verified -- you're connected." -ForegroundColor Green
            break
        }
        Write-Host ""
        Write-Host "That key could not be verified (see the message above)." -ForegroundColor Yellow
        $again = Read-OptionalValue "  Paste a different key to retry, or press Enter to start anyway"
        if ($again -eq "") {
            Write-Host "  Starting without a verified key -- paid runs will fail until it's fixed in System\.env." -ForegroundColor Yellow
            break
        }
        Set-EnvValue -Key "ANTHROPIC_API_KEY" -Value $again
    }
}

$env:PYTHONUNBUFFERED = "1"

# --- Start the server, open the browser once it's up -----------------------------
$port = if ($env:GNA_UI_PORT) { $env:GNA_UI_PORT } else { "8420" }
$url = "http://127.0.0.1:$port/"

Start-Job -ScriptBlock {
    param($TargetUrl)
    Start-Sleep -Seconds 2
    Start-Process $TargetUrl
} -ArgumentList $url | Out-Null

Write-Host "Starting the G&A Classifier UI at $url (Ctrl+C to stop)..." -ForegroundColor DarkGray

# Foreground, blocking: Ctrl+C here stops the server directly, same as any
# other local dev server.
& $python -u -m gna_server
exit $LASTEXITCODE
