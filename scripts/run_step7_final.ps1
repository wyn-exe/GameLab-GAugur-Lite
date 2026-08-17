[CmdletBinding()]
param(
    [ValidateRange(1, 48)]
    [int]$BatchSize = 24,
    [switch]$PreflightOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Push-Location $repoRoot
try {
    $forbiddenChanges = @(
        git status --short -- README.md gaugur_lite configs pyproject.toml scripts tests
    )
    if ($forbiddenChanges.Count -gt 0) {
        throw "Commit all Step 7 source/config changes before running:`n$($forbiddenChanges -join "`n")"
    }
    if ($PreflightOnly) {
        Write-Host '[Step 7 final] Verifying the sealed 2x5 pooled calibration...'
        python scripts\prepare_step7_pooled_calibration.py --verify-only
        if ($LASTEXITCODE -ne 0) {
            throw "Pooled calibration verification failed with exit code $LASTEXITCODE"
        }
        Write-Host '[Step 7 final] Formal profile preflight...'
        powershell.exe -NoProfile -ExecutionPolicy Bypass `
            -File scripts\run_step7_safety_v2_acceptance.ps1 `
            -BatchSize $BatchSize `
            -PreflightOnly
        if ($LASTEXITCODE -ne 0) {
            throw "Formal profile preflight failed with exit code $LASTEXITCODE"
        }
        return
    }

    Write-Host '[Step 7 final] Verifying pooled Candidate003+004 denominators (no new benchmark)...'
    python scripts\prepare_step7_pooled_calibration.py --verify-only
    if ($LASTEXITCODE -ne 0) {
        throw "Pooled calibration verification stopped Step 7 with exit code $LASTEXITCODE"
    }

    Write-Host '[Step 7 final] Running/resuming all 480 formal profile rows...'
    powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File scripts\run_step7_safety_v2_acceptance.ps1 `
        -BatchSize $BatchSize
    if ($LASTEXITCODE -ne 0) {
        throw "Formal profile collection stopped with exit code $LASTEXITCODE"
    }
    Write-Host '[Step 7 final] PASS: pooled calibration and all formal profile artifacts verified.'
}
finally {
    Pop-Location
}
