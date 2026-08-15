[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$artifactRoot = Join-Path $repoRoot 'artifacts\runner\step5'
$recoveryReport = Join-Path $artifactRoot 'recovery-run.json'
$resumeReport = Join-Path $artifactRoot 'resume-run.json'
$indexPath = Join-Path $artifactRoot 'formal-runs\step5-acceptance\step5-acceptance__extra__pyxel_bubbles+pyxel_jump+pyxel_shooter+pyxel_snake__r01\index.json'
$completedSummary = Join-Path $artifactRoot 'formal-runs\step5-acceptance\step5-acceptance__extra__pyxel_bubbles+pyxel_jump+pyxel_shooter+pyxel_snake__r01\attempts\a002\summary.json'
$acceptanceVerification = Join-Path $artifactRoot 'formal-acceptance-verification.json'

$required = @($recoveryReport, $resumeReport, $indexPath, $completedSummary)
$missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
if ($missing.Count -gt 0) {
    throw "Step 5 finalization prerequisite missing: $($missing -join ', ')"
}
if (Test-Path -LiteralPath $acceptanceVerification) {
    throw "Final verification output already exists; do not overwrite: $acceptanceVerification"
}

$recovery = Get-Content -LiteralPath $recoveryReport -Raw | ConvertFrom-Json
$resume = Get-Content -LiteralPath $resumeReport -Raw | ConvertFrom-Json
$index = Get-Content -LiteralPath $indexPath -Raw | ConvertFrom-Json
$summary = Get-Content -LiteralPath $completedSummary -Raw | ConvertFrom-Json
if ($recovery.status -ne 'passed' -or [int]$recovery.completed -ne 1) {
    throw 'recovery-run.json does not record one completed run'
}
if ($resume.status -ne 'passed' -or [int]$resume.skipped -ne 1) {
    throw 'resume-run.json does not record one safely skipped run'
}
if (@($index.attempts).Count -ne 2 `
        -or $index.attempts[0].status -ne 'failed' `
        -or $index.attempts[1].status -ne 'completed') {
    throw 'attempt history must preserve failed a001 followed by completed a002'
}
if ($summary.status -ne 'completed' -or $summary.valid -ne $true) {
    throw 'a002 summary is not completed and valid'
}

Push-Location $repoRoot
try {
    Write-Host '[Step 5 finalization] Running independent read-only checks; no game window will be opened...'
    $verificationRaw = python scripts\verify_step5_acceptance.py `
        --artifact-root $artifactRoot `
        --output $acceptanceVerification
    if ($LASTEXITCODE -ne 0) {
        $verificationRaw | Out-Host
        throw "Step 5 independent verification failed with exit code $LASTEXITCODE"
    }
    $result = ($verificationRaw | Out-String) | ConvertFrom-Json
    Write-Host ("PASS independent verification: {0}/{1} checks, formal plan SHA-256={2}" -f `
        $result.passed_count,
        $result.check_count,
        $result.formal_plan_sha256)
    Write-Host "Step 5 acceptance artifacts finalized at: $artifactRoot"
}
finally {
    Pop-Location
}
