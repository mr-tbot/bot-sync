# BOT-SYNC installer wrapper for Windows.
# Run from an *Administrator* PowerShell:
#   PS> Set-ExecutionPolicy -Scope Process Bypass -Force
#   PS> .\install.ps1
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]] $Args
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Fail($msg) {
    Write-Host "[bot-sync] $msg" -ForegroundColor Red
    exit 2
}

# -------------------------------------------------------------- Admin check
$needsAdmin = $true
foreach ($a in $Args) {
    if ($a -in @('--print-only','--uninstall')) { $needsAdmin = $false }
    if ($a -eq 'router') { $needsAdmin = $false }
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($needsAdmin -and -not $isAdmin) {
    Write-Host "[bot-sync] This installer must be run from an Administrator PowerShell." -ForegroundColor Red
    Write-Host "           Right-click PowerShell -> 'Run as administrator', then re-run:"
    Write-Host "             Set-ExecutionPolicy -Scope Process Bypass -Force"
    Write-Host "             .\install.ps1 $($Args -join ' ')"
    exit 2
}

# -------------------------------------------------------------- Python check
$py = $null
$pyVer = $null
foreach ($c in 'python','py','python3') {
    $found = Get-Command $c -ErrorAction SilentlyContinue
    if ($found) {
        try {
            $verOut = & $found.Source -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $verOut) {
                $parts = ($verOut.Trim() -split '\.')
                if ([int]$parts[0] -ge 3 -and ([int]$parts[0] -gt 3 -or [int]$parts[1] -ge 7)) {
                    $py = $found.Source
                    $pyVer = $verOut.Trim()
                    break
                }
            }
        } catch {}
    }
}

if (-not $py) {
    Write-Host "[bot-sync] Python 3.7+ is required and was not found on PATH." -ForegroundColor Red
    Write-Host ""
    Write-Host "Install Python with one of:"
    Write-Host "  winget install -e --id Python.Python.3.12"
    Write-Host "  choco install -y python"
    Write-Host "  https://www.python.org/downloads/windows/  (tick 'Add to PATH')"
    Write-Host ""
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $ans = Read-Host "Install Python 3.12 via winget now? [y/N]"
        if ($ans -match '^[Yy]') {
            winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
            Write-Host "[bot-sync] Open a new PowerShell so PATH refreshes, then re-run install.ps1." -ForegroundColor Yellow
        }
    }
    exit 2
}

Write-Host "[bot-sync] Using Python $pyVer at $py" -ForegroundColor Cyan

if (-not (Test-Path (Join-Path $here 'install.py'))) {
    Fail "install.py is missing next to install.ps1 — run from a bot-sync checkout."
}

# Sanity: schtasks must exist (it ships with Windows but corp policy can hide it).
if (-not (Get-Command schtasks -ErrorAction SilentlyContinue)) {
    Fail "schtasks.exe not on PATH (required to register the BOT-SYNC service)."
}

& $py (Join-Path $here 'install.py') @Args
exit $LASTEXITCODE
