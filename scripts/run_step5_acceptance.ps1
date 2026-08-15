[CmdletBinding()]
param(
    [switch]$PreflightOnly
)

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
$resumeReport = Join-Path $artifactRoot 'resume-run.json'
$acceptanceVerification = Join-Path $artifactRoot 'formal-acceptance-verification.json'
$unitLog = Join-Path $artifactRoot 'unit-tests.txt'
$formalRunRoot = Join-Path $artifactRoot 'formal-runs\step5-acceptance\step5-acceptance__extra__pyxel_bubbles+pyxel_jump+pyxel_shooter+pyxel_snake__r01'

$plannedOutputs = @(
    $formalPlan,
    $formalManifest,
    $formalCombinations,
    $formalVerification,
    $quadPlan,
    $quadManifest,
    $quadCombinations,
    $quadVerification,
    $firstReport,
    $resumeReport,
    $acceptanceVerification,
    $unitLog,
    $formalRunRoot
)
$existing = @($plannedOutputs | Where-Object { Test-Path -LiteralPath $_ })
if ($existing.Count -gt 0) {
    throw "Formal Step 5 output already exists; do not overwrite: $($existing -join ', ')"
}

if ($PreflightOnly) {
    [ordered]@{
        status = 'passed'
        powershell_version = $PSVersionTable.PSVersion.ToString()
        formal_plan_rows = 720
        formal_main_combinations = 60
        formal_extra_combinations = 12
        acceptance_runs = 1
        acceptance_workloads = 4
        acceptance_warmup_s = 2
        acceptance_duration_s = 8
        acceptance_cooldown_s = 2
        formal_outputs_exist = $false
    } | ConvertTo-Json
    return
}

New-Item -ItemType Directory -Force -Path $artifactRoot | Out-Null
Push-Location $repoRoot
try {
    Write-Host '[Step 5] Running unit tests...'
    $unitOutput = python -m pytest tests\unit -q -p no:cacheprovider --basetemp .test-tmp\step5-formal-unit 2>&1
    $unitExit = $LASTEXITCODE
    $unitOutput | Out-File -LiteralPath $unitLog -Encoding utf8
    $unitOutput | Out-Host
    if ($unitExit -ne 0) {
        throw "Unit tests failed with exit code $unitExit"
    }

    Write-Host '[Step 5] Generating and verifying the immutable 720-row formal plan...'
    $formalRaw = python -m gaugur_lite plan `
        --config configs\local.example.yaml `
        --experiment configs\experiments\formal.yaml `
        --workloads configs\workloads.yaml `
        --stage all `
        --out $formalPlan
    if ($LASTEXITCODE -ne 0) {
        $formalRaw | Out-Host
        throw "Formal plan generation failed with exit code $LASTEXITCODE"
    }
    $formalResult = ($formalRaw | Out-String) | ConvertFrom-Json
    Write-Host ("PASS formal plan: rows={0}, SHA-256={1}" -f `
        $formalResult.row_count,
        $formalResult.plan_sha256)

    $formalVerifyRaw = python -m gaugur_lite plan-verify `
        --plan $formalPlan `
        --output $formalVerification
    if ($LASTEXITCODE -ne 0) {
        $formalVerifyRaw | Out-Host
        throw "Formal plan verification failed with exit code $LASTEXITCODE"
    }
    $formalVerifyResult = ($formalVerifyRaw | Out-String) | ConvertFrom-Json
    Write-Host ("PASS formal plan verification: checks={0}" -f $formalVerifyResult.checks.Count)

    Write-Host '[Step 5] Generating the one-run visible four-window acceptance plan...'
    $quadRaw = python -m gaugur_lite plan `
        --config configs\step5.acceptance.yaml `
        --experiment configs\experiments\step5_acceptance.yaml `
        --workloads configs\workloads.yaml `
        --stage colocation-extra-test `
        --out $quadPlan
    if ($LASTEXITCODE -ne 0) {
        $quadRaw | Out-Host
        throw "Quad plan generation failed with exit code $LASTEXITCODE"
    }
    $quadResult = ($quadRaw | Out-String) | ConvertFrom-Json
    if ([int]$quadResult.row_count -ne 1) {
        throw "Quad acceptance plan must contain exactly one row"
    }
    $quadVerifyRaw = python -m gaugur_lite plan-verify `
        --plan $quadPlan `
        --output $quadVerification
    if ($LASTEXITCODE -ne 0) {
        $quadVerifyRaw | Out-Host
        throw "Quad plan verification failed with exit code $LASTEXITCODE"
    }

    Write-Host '[Step 5] Running four visible Pyxel windows (do not minimize or cover them)...'
    $firstRaw = python -m gaugur_lite run `
        --plan $quadPlan `
        --resume `
        --report $firstReport
    if ($LASTEXITCODE -ne 0) {
        $firstRaw | Out-Host
        throw "Visible quad run failed with exit code $LASTEXITCODE"
    }
    $firstResult = ($firstRaw | Out-String) | ConvertFrom-Json
    Write-Host ("PASS quad run: completed={0}, elapsed={1:N2}s" -f `
        $firstResult.completed,
        [double]$firstResult.elapsed_s)

    Write-Host '[Step 5] Re-running with --resume; no child or window may be created...'
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

    Write-Host '[Step 5] Independently checking plan counts, barriers, windows, hashes and PIDs...'
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
    Write-Host "Step 5 acceptance artifacts written to: $artifactRoot"
}
finally {
    Pop-Location
}
