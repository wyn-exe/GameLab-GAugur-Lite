[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'README.md') -PathType Leaf)) {
    throw "Repository marker README.md not found under $repoRoot"
}

$artifactRoot = Join-Path $repoRoot 'artifacts\runner\step5'
$formalPlan = Join-Path $artifactRoot 'formal-plan.csv'
$formalManifest = Join-Path $artifactRoot 'formal-plan-manifest.json'
$formalCombinations = Join-Path $artifactRoot 'formal-plan-combinations.json'
$formalVerification = Join-Path $artifactRoot 'formal-plan-verification.json'
$quadPlan = Join-Path $artifactRoot 'quad-plan.csv'
$quadManifest = Join-Path $artifactRoot 'quad-plan-manifest.json'
$quadCombinations = Join-Path $artifactRoot 'quad-plan-combinations.json'
$quadVerification = Join-Path $artifactRoot 'quad-plan-verification.json'
$firstReport = Join-Path $artifactRoot 'first-run.json'
$recoveryReport = Join-Path $artifactRoot 'recovery-run.json'
$resumeReport = Join-Path $artifactRoot 'resume-run.json'
$acceptanceVerification = Join-Path $artifactRoot 'formal-acceptance-verification.json'
$recoveryUnitLog = Join-Path $artifactRoot 'recovery-unit-tests.txt'
$runRoot = Join-Path $artifactRoot 'formal-runs\step5-acceptance\step5-acceptance__extra__pyxel_bubbles+pyxel_jump+pyxel_shooter+pyxel_snake__r01'
$indexPath = Join-Path $runRoot 'index.json'
$failedAttempt = Join-Path $runRoot 'attempts\a001\failure.json'

$required = @(
    $formalPlan,
    $formalManifest,
    $formalCombinations,
    $formalVerification,
    $quadPlan,
    $quadManifest,
    $quadCombinations,
    $quadVerification,
    $firstReport,
    $indexPath,
    $failedAttempt
)
$missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
if ($missing.Count -gt 0) {
    throw "Step 5 recovery prerequisite missing: $($missing -join ', ')"
}

$plannedOutputs = @(
    $recoveryReport,
    $resumeReport,
    $acceptanceVerification,
    $recoveryUnitLog
)
$existing = @($plannedOutputs | Where-Object { Test-Path -LiteralPath $_ })
if ($existing.Count -gt 0) {
    throw "Step 5 recovery output already exists; do not overwrite: $($existing -join ', ')"
}

$first = Get-Content -LiteralPath $firstReport -Raw | ConvertFrom-Json
$index = Get-Content -LiteralPath $indexPath -Raw | ConvertFrom-Json
if ($first.status -ne 'failed' -or [int]$first.failed_or_invalid -ne 1) {
    throw 'first-run.json is not the preserved failed run expected by this recovery script'
}
if (@($index.attempts).Count -ne 1 -or $index.attempts[0].status -ne 'failed') {
    throw 'index.json must contain only the preserved failed a001 before recovery'
}

Push-Location $repoRoot
try {
    Write-Host '[Step 5 recovery] Running unit tests...'
    $unitOutput = python -m pytest tests\unit -q -p no:cacheprovider --basetemp .test-tmp\step5-recovery-unit 2>&1
    $unitExit = $LASTEXITCODE
    $unitOutput | Out-File -LiteralPath $recoveryUnitLog -Encoding utf8
    $unitOutput | Out-Host
    if ($unitExit -ne 0) {
        throw "Unit tests failed with exit code $unitExit"
    }

    Write-Host '[Step 5 recovery] Read-only verification of the existing immutable plans...'
    $formalRaw = python -m gaugur_lite plan-verify --plan $formalPlan
    if ($LASTEXITCODE -ne 0) {
        $formalRaw | Out-Host
        throw "Formal plan verification failed with exit code $LASTEXITCODE"
    }
    $formalResult = ($formalRaw | Out-String) | ConvertFrom-Json
    $quadRaw = python -m gaugur_lite plan-verify --plan $quadPlan
    if ($LASTEXITCODE -ne 0) {
        $quadRaw | Out-Host
        throw "Quad plan verification failed with exit code $LASTEXITCODE"
    }
    $quadResult = ($quadRaw | Out-String) | ConvertFrom-Json
    Write-Host ("PASS immutable plans: formal rows={0}, quad rows={1}" -f `
        $formalResult.row_count,
        $quadResult.row_count)

    $dryRaw = python -m gaugur_lite run --plan $quadPlan --resume --dry-run
    if ($LASTEXITCODE -ne 0) {
        $dryRaw | Out-Host
        throw "Recovery dry-run failed with exit code $LASTEXITCODE"
    }
    $dryResult = ($dryRaw | Out-String) | ConvertFrom-Json
    if ([int]$dryResult.would_run -ne 1 -or [int]$dryResult.decisions[0].attempt -ne 2) {
        throw 'Recovery dry-run did not select exactly attempt a002'
    }

    Write-Host '[Step 5 recovery] Running a002 with four visible Pyxel windows (do not minimize or cover them)...'
    $recoveryRaw = python -m gaugur_lite run `
        --plan $quadPlan `
        --resume `
        --report $recoveryReport
    if ($LASTEXITCODE -ne 0) {
        $recoveryRaw | Out-Host
        throw "Visible quad recovery run failed with exit code $LASTEXITCODE"
    }
    $recoveryResult = ($recoveryRaw | Out-String) | ConvertFrom-Json
    Write-Host ("PASS recovery: attempt={0}, completed={1}, elapsed={2:N2}s" -f `
        $recoveryResult.results[0].attempt,
        $recoveryResult.completed,
        [double]$recoveryResult.elapsed_s)

    Write-Host '[Step 5 recovery] Re-running with --resume; no child or window may be created...'
    $resumeRaw = python -m gaugur_lite run `
        --plan $quadPlan `
        --resume `
        --report $resumeReport
    if ($LASTEXITCODE -ne 0) {
        $resumeRaw | Out-Host
        throw "Resume verification run failed with exit code $LASTEXITCODE"
    }
    $resumeResult = ($resumeRaw | Out-String) | ConvertFrom-Json
    Write-Host ("PASS resume: skipped={0}, completed={1}" -f `
        $resumeResult.skipped,
        $resumeResult.completed)

    Write-Host '[Step 5 recovery] Independently checking a001 preservation, a002, hashes, windows and PIDs...'
    $acceptanceRaw = python scripts\verify_step5_acceptance.py `
        --artifact-root $artifactRoot `
        --output $acceptanceVerification
    if ($LASTEXITCODE -ne 0) {
        $acceptanceRaw | Out-Host
        throw "Step 5 independent verification failed with exit code $LASTEXITCODE"
    }
    $acceptanceResult = ($acceptanceRaw | Out-String) | ConvertFrom-Json
    Write-Host ("PASS independent verification: {0}/{1} checks, formal plan SHA-256={2}" -f `
        $acceptanceResult.passed_count,
        $acceptanceResult.check_count,
        $acceptanceResult.formal_plan_sha256)
    Write-Host "Step 5 recovery artifacts written to: $artifactRoot"
}
finally {
    Pop-Location
}
