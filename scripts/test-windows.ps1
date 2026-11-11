# Run this repo's tests on Windows, then print a result block to paste back.
#
#   powershell -ExecutionPolicy Bypass -File .\scripts\test-windows.ps1
#
# It creates .venv-win, installs the dependencies of every project that owns a tests directory,
# runs each suite, and writes RESULT.txt. Nothing outside this folder is touched.

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

function Section($t) { Write-Host ""; Write-Host "== $t ==" -ForegroundColor Cyan }

Section "machine"
$os = [System.Environment]::OSVersion.VersionString
Write-Host "host   : $env:COMPUTERNAME"
Write-Host "os     : $os"
Write-Host "shell  : PowerShell $($PSVersionTable.PSVersion)"

Section "python"
$py = $null
foreach ($cand in @("python", "py", "python3")) {
    $p = Get-Command $cand -ErrorAction SilentlyContinue
    if ($p) { $py = $p.Source; break }
}
if (-not $py) {
    Write-Host "No Python found. Install Python 3.12 from python.org and tick 'Add python.exe to PATH'." -ForegroundColor Red
    exit 2
}
& $py --version
$pyVersion = (& $py --version 2>&1) -join " "

Section "virtual environment"
# Outside the repo on purpose: an installed plugin must never carry 92 MB of venv symlinks, which is
# what once blocked the next deploy ("links are not followed into an update").
$venvDir = Join-Path $env:TEMP "hail-hermes-venv-win"
if (-not (Test-Path $venvDir)) { & $py -m venv $venvDir }
$venvPy = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Write-Host "venv creation failed" -ForegroundColor Red; exit 2 }

Section "finding tests"
$suites = Get-ChildItem -Recurse -Directory -Filter "tests" |
    Where-Object { $_.FullName -notmatch "node_modules|\.venv|site-packages|__pycache__|\\tools\\pane" } |
    ForEach-Object { $_.FullName } | Sort-Object
if (-not $suites) { Write-Host "No tests directory found anywhere in this repo." -ForegroundColor Yellow }
$suites | ForEach-Object { Write-Host "  $_" }

# Install the dependencies of the project each suite belongs to — the directory one level up.
# This repo has no manifest at its root: they live in plugin\ and tools\*\ , and some test-only
# packages (numpy) are declared only in the `test` extra. A root-only install leaves a fresh
# machine unable to import the tests at all, which looks like a code failure and is not one.
Section "dependencies"
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet pytest
$seen = @()
foreach ($suite in $suites) {
    $proj = Split-Path $suite -Parent
    if ($seen -contains $proj) { continue }
    $seen += $proj
    foreach ($f in @("requirements.txt", "requirements-dev.txt")) {
        $rf = Join-Path $proj $f
        if (Test-Path $rf) { Write-Host "installing $rf"; & $venvPy -m pip install --quiet -r $rf }
    }
    if (Test-Path (Join-Path $proj "pyproject.toml")) {
        Write-Host "installing $proj (editable, test extra)"
        & $venvPy -m pip install --quiet -e "${proj}[test]" 2>&1 | Select-Object -Last 2
        if ($LASTEXITCODE -ne 0) { & $venvPy -m pip install --quiet -e $proj 2>&1 | Select-Object -Last 2 }
    }
}

Section "running"
$summary = @()
$overall = 0
foreach ($suite in $suites) {
    $rel = $suite.Substring($root.Length).TrimStart("\")
    $line = "---- $rel ----"
    Write-Host $line -ForegroundColor Yellow
    $out = & $venvPy -m pytest $suite -o addopts="" -q -p no:cacheprovider 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0 -and $code -ne 5) { $overall = $code }
    $out | Select-Object -Last 12 | ForEach-Object { Write-Host $_ }
    $tail = ($out | Select-String -Pattern "\d+ (passed|failed|error)" | Select-Object -Last 1).Line
    if (-not $tail) { $tail = "exit $code, no summary line" }
    $summary += "$rel :: $tail"
}

Section "git"
$rev = ""
if (Get-Command git -ErrorAction SilentlyContinue) { $rev = (& git rev-parse --short HEAD 2>$null) }

$report = @()
$report += "plugin:    $((Split-Path $root -Leaf))"
$report += "host:      $env:COMPUTERNAME"
$report += "os:        $os"
$report += "python:    $pyVersion"
$report += "commit:    $rev"
$report += "exit:      $overall"
$report += "suites:"
$summary | ForEach-Object { $report += "  $_" }
$report += "run at:    $(Get-Date -Format o)"
$report | Set-Content -Path (Join-Path $root "RESULT.txt")

Section "RESULT"
$report | ForEach-Object { Write-Host $_ }
Write-Host ""
Write-Host "RESULT.txt written to $root\RESULT.txt - send that file back." -ForegroundColor Green
exit $overall
