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
