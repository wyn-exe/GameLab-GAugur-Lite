[CmdletBinding()]
param(
    [ValidateRange(1, 36)]
    [int]$BatchSize = 12,
    [switch]$PreflightOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pwsh = (Get-Command pwsh.exe -ErrorAction Stop).Source
Push-Location $repoRoot
try {
    if ($PreflightOnly) {
        Write-Host '[Step 8 final] Running readonly frozen-plan and resume preflight...'
        & $pwsh -NoProfile -ExecutionPolicy Bypass `
            -File scripts\run_step8_acceptance.ps1 `
            -BatchSize $BatchSize `
            -PreflightOnly
        if (-not $?) {
            throw 'Step 8 preflight stopped.'
        }
        return
    }

    Write-Host '[Step 8 final] Running/resuming all main and four-workload colocation rows...'
    & $pwsh -NoProfile -ExecutionPolicy Bypass `
        -File scripts\run_step8_acceptance.ps1 `
        -BatchSize $BatchSize
    if (-not $?) {
        throw 'Step 8 formal collection stopped.'
    }
    Write-Host '[Step 8 final] PASS: all formal colocation attempts and truth artifacts verified.'
}
finally {
    Pop-Location
}
