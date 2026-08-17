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
        Write-Host '[Step 7 final] Candidate004 preflight...'
        powershell.exe -NoProfile -ExecutionPolicy Bypass `
            -File scripts\run_step7_candidate004_calibration.ps1 `
            -PreflightOnly
        if ($LASTEXITCODE -ne 0) {
            throw "Candidate004 preflight failed with exit code $LASTEXITCODE"
        }
        $calibration = Join-Path $repoRoot 'artifacts\calibration\step7-safety-v2\formal-calibration-stable-v2.json'
        if (Test-Path -LiteralPath $calibration -PathType Leaf) {
            Write-Host '[Step 7 final] Formal profile preflight...'
            powershell.exe -NoProfile -ExecutionPolicy Bypass `
                -File scripts\run_step7_safety_v2_acceptance.ps1 `
                -BatchSize $BatchSize `
                -PreflightOnly
            if ($LASTEXITCODE -ne 0) {
                throw "Formal profile preflight failed with exit code $LASTEXITCODE"
            }
        }
        else {
            Write-Host '[Step 7 final] Formal profile preflight is deferred until Candidate004 exists.'
        }
        return
    }

    Write-Host '[Step 7 final] Building or verifying Candidate004...'
    powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File scripts\run_step7_candidate004_calibration.ps1
    if ($LASTEXITCODE -ne 0) {
        throw "Candidate004 stopped Step 7 with exit code $LASTEXITCODE"
    }

    Write-Host '[Step 7 final] Running/resuming all 480 formal profile rows...'
    powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File scripts\run_step7_safety_v2_acceptance.ps1 `
        -BatchSize $BatchSize
    if ($LASTEXITCODE -ne 0) {
        throw "Formal profile collection stopped with exit code $LASTEXITCODE"
    }
    Write-Host '[Step 7 final] PASS: Candidate004 and all formal profile artifacts verified.'
}
finally {
    Pop-Location
}
