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

$plan = Join-Path $repoRoot 'artifacts\plans\formal-v1.csv'
$planManifest = Join-Path $repoRoot 'artifacts\plans\formal-v1-manifest.json'
$planCombinations = Join-Path $repoRoot 'artifacts\plans\formal-v1-combinations.json'
$planVerification = Join-Path $repoRoot 'artifacts\plans\formal-v1-verification.json'
$artifactRoot = Join-Path $repoRoot 'artifacts\baselines\step6'
$invocationRoot = Join-Path $artifactRoot 'invocations'
$summary = Join-Path $repoRoot 'data\interim\formal-v1\solo-baselines.json'
$runs = Join-Path $repoRoot 'data\interim\formal-v1\solo-runs.jsonl'
$plot = Join-Path $artifactRoot 'formal-solo-baselines.png'
$verification = Join-Path $artifactRoot 'formal-solo-verification.json'

$required = @($plan, $planManifest, $planCombinations)
$missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
if ($missing.Count -gt 0) {
    throw "Frozen formal acquisition plan is incomplete: $($missing -join ', ')"
}
$manifest = Get-Content -LiteralPath $planManifest -Raw | ConvertFrom-Json
$actualPlanHash = (Get-FileHash -LiteralPath $plan -Algorithm SHA256).Hash.ToLowerInvariant()
if ([int]$manifest.row_count -ne 720 `
        -or $manifest.root_dirty_at_generation -ne $false `
        -or $actualPlanHash -ne $manifest.plan_sha256) {
    throw 'Frozen formal plan must contain 720 rows, come from a clean commit and match its SHA-256'
}
$rows = @(Import-Csv -LiteralPath $plan)
$soloRows = @($rows | Where-Object { $_.stage -eq 'solo' })
if ($soloRows.Count -ne 24) {
    throw "Frozen formal plan must contain exactly 24 solo rows; actual=$($soloRows.Count)"
}

if (Test-Path -LiteralPath $verification) {
    throw "Step 6 is already finalized; do not rerun or overwrite: $verification"
}
$partialFinalOutputs = @(@($summary, $runs, $plot) | Where-Object { Test-Path -LiteralPath $_ })
if ($partialFinalOutputs.Count -gt 0) {
    throw "Partial Step 6 summary output exists; stop for audit instead of overwriting: $($partialFinalOutputs -join ', ')"
}

if ($PreflightOnly) {
    [ordered]@{
        status = 'passed'
        plan_rows = 720
        solo_rows = 24
        workload_count = 8
        repeats_per_workload = 3
        warmup_s = 20
        duration_s = 60
        cooldown_s = 20
        estimated_minutes = 40
        plan_sha256 = $actualPlanHash
        plan_root_commit = $manifest.root_commit
        plan_root_dirty = $manifest.root_dirty_at_generation
        final_outputs_exist = $false
    } | ConvertTo-Json
    return
}

New-Item -ItemType Directory -Force -Path $artifactRoot | Out-Null
New-Item -ItemType Directory -Force -Path $invocationRoot | Out-Null
$invocationNumber = 1
while ($true) {
    $invocationTag = 'invocation-{0:D3}' -f $invocationNumber
    $runReport = Join-Path $invocationRoot "$invocationTag-run.json"
    $unitLog = Join-Path $invocationRoot "$invocationTag-unit-tests.txt"
    if (-not (Test-Path -LiteralPath $runReport) `
            -and -not (Test-Path -LiteralPath $unitLog)) {
        break
    }
    $invocationNumber += 1
}

Push-Location $repoRoot
try {
    Write-Host "[Step 6/$invocationTag] Running unit tests..."
    $unitOutput = python -m pytest tests\unit -q -p no:cacheprovider `
        --basetemp ".test-tmp\step6-$invocationTag-unit" 2>&1
    $unitExit = $LASTEXITCODE
    $unitOutput | Out-File -LiteralPath $unitLog -Encoding utf8
    $unitOutput | Out-Host
    if ($unitExit -ne 0) {
        throw "Unit tests failed with exit code $unitExit"
    }

    Write-Host '[Step 6] Verifying the frozen clean-commit 720-row plan...'
    if (Test-Path -LiteralPath $planVerification) {
        $planRaw = python -m gaugur_lite plan-verify --plan $plan
    }
    else {
        $planRaw = python -m gaugur_lite plan-verify `
            --plan $plan `
            --output $planVerification
    }
    if ($LASTEXITCODE -ne 0) {
        $planRaw | Out-Host
        throw "Plan verification failed with exit code $LASTEXITCODE"
    }
    $planResult = ($planRaw | Out-String) | ConvertFrom-Json
    Write-Host ("PASS formal plan: rows={0}, checks={1}, SHA-256={2}" -f `
        $planResult.row_count,
        $planResult.checks.Count,
        $planResult.plan_sha256)

    Write-Host '[Step 6] Computing safe resume decisions for the 24 solo rows...'
    $dryRaw = python -m gaugur_lite run `
        --plan $plan `
        --stage solo `
        --resume `
        --dry-run
    if ($LASTEXITCODE -ne 0) {
        $dryRaw | Out-Host
        throw "Solo dry-run failed with exit code $LASTEXITCODE"
    }
    $dryResult = ($dryRaw | Out-String) | ConvertFrom-Json
    if ([int]$dryResult.selected_runs -ne 24 `
            -or ([int]$dryResult.would_run + [int]$dryResult.would_skip) -ne 24) {
        throw 'Solo dry-run must select exactly 24 rows'
    }
    Write-Host ("PASS resume preflight: would_run={0}, would_skip={1}" -f `
        $dryResult.would_run,
        $dryResult.would_skip)

    Write-Host '[Step 6] Running/resuming 24 visible solo runs (about 40 minutes from empty state)...'
    Write-Host '[Step 6] Keep each Pyxel window visible and unobscured; do not minimize it.'
    $runRaw = python -m gaugur_lite run `
        --plan $plan `
        --stage solo `
        --resume `
        --report $runReport
    $runExit = $LASTEXITCODE
    if ($runExit -ne 0) {
        $runRaw | Out-Host
        throw "Solo runner has failed/invalid attempts (exit $runExit); keep artifacts and rerun this script only after audit"
    }
    $runResult = ($runRaw | Out-String) | ConvertFrom-Json
    if ([int]$runResult.failed_or_invalid -ne 0 `
            -or ([int]$runResult.completed + [int]$runResult.skipped) -ne 24) {
        throw 'Solo runner did not finish/skip exactly 24 valid runs'
    }
    Write-Host ("PASS solo runner: completed={0}, skipped={1}, elapsed={2:N2}s" -f `
        $runResult.completed,
        $runResult.skipped,
        [double]$runResult.elapsed_s)

    Write-Host '[Step 6] Building 8 workload baselines and repeat-stability plot...'
    $summaryRaw = python -m gaugur_lite summarize `
        --plan $plan `
        --stage solo `
        --out $summary `
        --runs-out $runs `
        --plot $plot `
        --fps-cv-threshold-pct 5
    if ($LASTEXITCODE -ne 0) {
        $summaryRaw | Out-Host
        throw "Solo summarization failed with exit code $LASTEXITCODE"
    }
    $summaryResult = ($summaryRaw | Out-String) | ConvertFrom-Json
    Write-Host ("PASS baselines: workloads={0}, runs={1}, max CV={2:N4}%" -f `
        $summaryResult.workload_count,
        $summaryResult.run_count,
        [double](($summaryResult.baselines | Measure-Object -Property mean_fps_cv_pct -Maximum).Maximum))

    Write-Host '[Step 6] Recomputing baselines from raw attempts and verifying JSONL/PNG hashes...'
    $verifyRaw = python -m gaugur_lite summarize-verify `
        --plan $plan `
        --summary $summary `
        --runs $runs `
        --plot $plot `
        --output $verification
    if ($LASTEXITCODE -ne 0) {
        $verifyRaw | Out-Host
        throw "Solo baseline verification failed with exit code $LASTEXITCODE"
    }
    $verifyResult = ($verifyRaw | Out-String) | ConvertFrom-Json
    Write-Host ("PASS independent verification: {0}/{1} checks, summary SHA-256={2}" -f `
        $verifyResult.passed_count,
        $verifyResult.check_count,
        $verifyResult.summary_sha256)
    Write-Host "Step 6 acceptance artifacts written to: $artifactRoot"
}
finally {
    Pop-Location
}
