<#!
.SYNOPSIS
Bootstrap Medios-Macina on Windows.

.DESCRIPTION
When run from a checkout, this wrapper delegates to scripts/bootstrap.py. When
the script is piped from the web, it downloads the canonical Python bootstrap
to a temporary file so a checkout is not required first.

.EXAMPLE
.\scripts\bootstrap.ps1

.EXAMPLE
.\scripts\bootstrap.ps1 -Editable -NoPlaywright
#>

param(
    [switch]$Editable,
    [switch]$CreateDesktopShortcut,
    [switch]$CreateStartMenuShortcut,
    [string]$VenvPath = "",
    [string]$Python = "",
    [switch]$Force,
    [switch]$NoInstall,
    [switch]$NoMpv,
    [switch]$NoDeno,
    [switch]$NoPlaywright,
    [string]$PlaywrightBrowsers = "chromium",
    [switch]$FixUrllib3,
    [switch]$RemovePth,
    [switch]$Quiet,
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$bootstrapUrl = [string]$env:MM_BOOTSTRAP_URL
$scriptPath = $MyInvocation.MyCommand.Path
$repoRoot = $null
$bootstrapPath = $null
$temporaryBootstrap = $false

if ($scriptPath) {
    $scriptDir = Split-Path -Parent $scriptPath
    $candidateRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
    if ((Test-Path (Join-Path $candidateRoot "CLI.py")) -and (Test-Path (Join-Path $candidateRoot "scripts\bootstrap.py"))) {
        $repoRoot = $candidateRoot
        $bootstrapPath = Join-Path $repoRoot "scripts\bootstrap.py"
    }
}

if (-not $bootstrapPath) {
    if (-not $bootstrapUrl) {
        throw "Run this script from a Medeia-Macina checkout, or set MM_BOOTSTRAP_URL."
    }
    $bootstrapPath = Join-Path ([IO.Path]::GetTempPath()) ("medios-macina-bootstrap-{0}.py" -f ([guid]::NewGuid()))
    Invoke-WebRequest -Uri $bootstrapUrl -OutFile $bootstrapPath -UseBasicParsing
    $temporaryBootstrap = $true
}

$pythonArgs = @()
if ($Editable) { $pythonArgs += "--install-editable" }
if ($NoInstall) { $pythonArgs += "--skip-deps" }
if ($NoMpv) { $pythonArgs += "--no-mpv" }
if ($NoDeno) { $pythonArgs += "--no-deno" }
if ($NoPlaywright) { $pythonArgs += "--no-playwright" }
if ($PlaywrightBrowsers) { $pythonArgs += @("--browsers", $PlaywrightBrowsers) }
if ($Quiet) { $pythonArgs += "--quiet" }
if ($Force) { $pythonArgs += "--yes" }

if ($VenvPath) {
    Write-Warning "-VenvPath is handled by the Python bootstrap's repository-local .venv setting."
}
if ($CreateDesktopShortcut -or $CreateStartMenuShortcut -or $FixUrllib3 -or $RemovePth) {
    Write-Warning "One or more legacy PowerShell-only options were ignored by the canonical Python bootstrap."
}

try {
    $exitCode = 1
    $pythonCommand = $null
    if ($Python) {
        $pythonCommand = $Python
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $pythonCommand = "py"
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $pythonCommand = "python"
    }

    if (-not $pythonCommand) {
        throw "No Python 3 interpreter found. Install Python 3 and run the bootstrap again."
    }

    if ($pythonCommand -eq "py") {
        & py -3 $bootstrapPath @pythonArgs
    } else {
        & $pythonCommand $bootstrapPath @pythonArgs
    }
    $exitCode = $LASTEXITCODE
    if (-not $Quiet) {
        if ($exitCode -eq 0) {
            Write-Host "Installation finished successfully." -ForegroundColor Green
        } else {
            Write-Host "Installation exited with code $exitCode." -ForegroundColor Red
        }
    }
} finally {
    if ($temporaryBootstrap -and (Test-Path $bootstrapPath)) {
        Remove-Item -LiteralPath $bootstrapPath -Force -ErrorAction SilentlyContinue
    }
}

if (-not $NoPause -and -not $Quiet -and [Environment]::UserInteractive) {
    try {
        [void](Read-Host "Press Enter to close installer")
    } catch {
        # Ignore host/input edge cases and continue to exit.
    }
}

exit $exitCode