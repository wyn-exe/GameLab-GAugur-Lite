[CmdletBinding()]
param(
    [ValidateRange(1, 48)]
    [int]$BatchSize = 24,
    [switch]$PreflightOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'README.md') -PathType Leaf)) {
    throw "Repository marker README.md not found under $repoRoot"
}

$plan = Join-Path $repoRoot 'artifacts\plans\formal-v1-profile-t84.csv'
$planManifest = Join-Path $repoRoot 'artifacts\plans\formal-v1-profile-t84-manifest.json'
$baselinePlan = Join-Path $repoRoot 'artifacts\plans\formal-v1.csv'
$soloBaselines = Join-Path $repoRoot 'data\interim\formal-v1\solo-baselines.json'
$calibration = Join-Path $repoRoot 'artifacts\calibration\step4\formal-calibration.json'
$amendment = Join-Path $repoRoot 'artifacts\profiles\step7\thermal-amendment.json'
$artifactRoot = Join-Path $repoRoot 'artifacts\profiles\step7\t84'
$invocationRoot = Join-Path $artifactRoot 'invocations'
$plotRoot = Join-Path $artifactRoot 'plots'
$planVerification = Join-Path $artifactRoot 'formal-v1-profile-t84-verification.json'
$profiles = Join-Path $repoRoot 'data\interim\formal-v1\profiles.parquet'
$profileRuns = Join-Path $repoRoot 'data\interim\formal-v1\profile-runs.jsonl'
$profileSummary = Join-Path $repoRoot 'data\interim\formal-v1\profile-summary.json'
$profileVerification = Join-Path $artifactRoot 'formal-profile-verification.json'

$required = @($plan, $planManifest, $baselinePlan, $soloBaselines, $calibration, $amendment)
$missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
if ($missing.Count -gt 0) {
    throw "Step 7 input is incomplete: $($missing -join ', ')"
}
$manifest = Get-Content -LiteralPath $planManifest -Raw | ConvertFrom-Json
$actualPlanHash = (Get-FileHash -LiteralPath $plan -Algorithm SHA256).Hash.ToLowerInvariant()
if ([int]$manifest.row_count -ne 480 `
        -or $manifest.selected_stage -ne 'profile' `
        -or $manifest.root_dirty_at_generation -ne $false `
        -or $actualPlanHash -ne $manifest.plan_sha256) {
    throw 'Frozen t84 plan must contain 480 profile rows, come from a clean commit and match its SHA-256'
}
$rows = @(Import-Csv -LiteralPath $plan)
$profileRows = @($rows | Where-Object { $_.stage -eq 'profile' })
if ($profileRows.Count -ne 480) {
    throw "Frozen formal plan must contain exactly 480 profile rows; actual=$($profileRows.Count)"
}

Push-Location $repoRoot
try {
    Write-Host '[Step 7] Verifying the recorded 82°C -> 84°C protocol amendment...'
    $amendmentRaw = python scripts\build_step7_thermal_amendment.py --output $amendment
    if ($LASTEXITCODE -ne 0) {
        $amendmentRaw | Out-Host
        throw "Thermal amendment verification failed with exit code $LASTEXITCODE"
    }

    Write-Host '[Step 7] Auditing immutable plan, solo FPS denominators and standalone benchmark denominators...'
    $inputRaw = python -m gaugur_lite features build-profiles `
        --plan $plan `
        --baseline-plan $baselinePlan `
        --solo-baselines $soloBaselines `
        --calibration $calibration `
        --out $profiles `
        --runs-out $profileRuns `
        --summary $profileSummary `
        --plot-dir $plotRoot `
        --dry-run
    if ($LASTEXITCODE -ne 0) {
        $inputRaw | Out-Host
        throw "Profile input audit failed with exit code $LASTEXITCODE"
    }
    $inputResult = ($inputRaw | Out-String) | ConvertFrom-Json
    Write-Host ("PASS inputs: profile rows={0}, standalone cells={1}, max throughput CV={2:N4}%" -f `
        $inputResult.profile_plan_rows,
        $inputResult.standalone_nonzero_cell_count,
        [double]$inputResult.standalone_throughput_cv_max_pct)

    Write-Host '[Step 7] Computing global safe-resume progress and source-tree lock...'
    $progressRaw = python -m gaugur_lite run `
        --plan $plan `
        --stage profile `
        --resume `
        --dry-run
    if ($LASTEXITCODE -ne 0) {
        $progressRaw | Out-Host
        throw "Profile progress preflight failed with exit code $LASTEXITCODE"
    }
    $progressResult = ($progressRaw | Out-String) | ConvertFrom-Json
    if ([int]$progressResult.selected_runs -ne 480) {
        throw 'Profile progress must inspect exactly 480 rows'
    }
    Write-Host ("PASS progress: completed={0}/480, remaining={1}" -f `
        $progressResult.progress.stage_completed_runs,
        $progressResult.progress.stage_remaining_runs)

    if ($PreflightOnly) {
        [ordered]@{
            status = 'passed'
            plan_sha256 = $actualPlanHash
            profile_rows = 480
            completed = [int]$progressResult.progress.stage_completed_runs
            remaining = [int]$progressResult.progress.stage_remaining_runs
            batch_size = $BatchSize
            total_batches = [Math]::Ceiling(480 / $BatchSize)
            estimated_minutes_per_full_batch = [Math]::Ceiling($BatchSize * 100 / 60)
            root_commit = $progressResult.execution_provenance.root_commit
            source_tree_sha256 = $progressResult.execution_provenance.source_tree_sha256
            existing_source_tree_sha256s = @($progressResult.progress.existing_source_tree_sha256s)
            existing_root_commits = @($progressResult.progress.existing_root_commits_by_stage.profile)
        } | ConvertTo-Json -Depth 4
        return
    }

    if (Test-Path -LiteralPath $profileVerification -PathType Leaf) {
        $storedVerification = Get-Content -LiteralPath $profileVerification -Raw | ConvertFrom-Json
        if ($storedVerification.status -ne 'passed') {
            throw "Existing Step 7 verification is not passed: $profileVerification"
        }
        Write-Host "Step 7 was already finalized at: $artifactRoot"
        return
    }

    New-Item -ItemType Directory -Force -Path $artifactRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $invocationRoot | Out-Null
    $invocationNumber = 1
    while ($true) {
        $invocationTag = 'invocation-{0:D3}' -f $invocationNumber
        $unitLog = Join-Path $invocationRoot "$invocationTag-unit-tests.txt"
        $progressReport = Join-Path $invocationRoot "$invocationTag-progress.json"
        if (-not (Test-Path -LiteralPath $unitLog) `
                -and -not (Test-Path -LiteralPath $progressReport)) {
            break
        }
        $invocationNumber += 1
    }

    Write-Host "[Step 7/$invocationTag] Running unit tests..."
    $unitOutput = python -m pytest tests\unit -q -p no:cacheprovider `
        --basetemp ".test-tmp\step7-$invocationTag-unit" 2>&1
    $unitExit = $LASTEXITCODE
    $unitOutput | Out-File -LiteralPath $unitLog -Encoding utf8
    $unitOutput | Out-Host
    if ($unitExit -ne 0) {
        throw "Unit tests failed with exit code $unitExit"
    }

    Write-Host '[Step 7] Verifying the frozen clean-commit 480-row t84 profile plan...'
    if (Test-Path -LiteralPath $planVerification) {
        $planRaw = python -m gaugur_lite plan-verify --plan $plan
    }
    else {
        $planRaw = python -m gaugur_lite plan-verify --plan $plan --output $planVerification
    }
    if ($LASTEXITCODE -ne 0) {
        $planRaw | Out-Host
        throw "Plan verification failed with exit code $LASTEXITCODE"
    }

    $remainingBefore = [int]$progressResult.progress.stage_remaining_runs
    $batchNumber = $null
    $runReport = $null
    if ($remainingBefore -gt 0) {
        $firstPendingIndex = -1
        for ($index = 0; $index -lt $progressResult.decisions.Count; $index++) {
            if ($progressResult.decisions[$index].action -eq 'run') {
                $firstPendingIndex = $index
                break
            }
        }
        if ($firstPendingIndex -lt 0) {
            throw 'Progress says rows remain, but no pending decision was found'
        }
        $batchNumber = [int][Math]::Floor($firstPendingIndex / $BatchSize) + 1
        $runReport = Join-Path $invocationRoot ("{0}-batch-{1:D3}-run.json" -f $invocationTag, $batchNumber)
        Write-Host ("[Step 7] Running/resuming batch {0}/{1}: at most {2} visible profile runs (~{3} minutes plus thermal extension)..." -f `
            $batchNumber,
            [Math]::Ceiling(480 / $BatchSize),
            $BatchSize,
            [Math]::Ceiling($BatchSize * 100 / 60))
        Write-Host '[Step 7] Keep the Pyxel window visible and unobscured; do not minimize it.'
        $runRaw = python -m gaugur_lite run `
            --plan $plan `
            --stage profile `
            --resume `
            --batch-number $batchNumber `
            --batch-size $BatchSize `
            --report $runReport
        $runExit = $LASTEXITCODE
        if ($runExit -ne 0) {
            $runRaw | Out-Host
            throw "Profile batch has failed/invalid attempts (exit $runExit); artifacts are preserved, rerun this same script after audit"
        }
        $runResult = ($runRaw | Out-String) | ConvertFrom-Json
        if ([int]$runResult.failed_or_invalid -ne 0) {
            throw 'Profile batch contains failed/invalid attempts despite a zero exit code'
        }
    }

    $afterRaw = python -m gaugur_lite run `
        --plan $plan `
        --stage profile `
        --resume `
        --dry-run
    if ($LASTEXITCODE -ne 0) {
        $afterRaw | Out-Host
        throw "Post-batch progress failed with exit code $LASTEXITCODE"
    }
    $after = ($afterRaw | Out-String) | ConvertFrom-Json
    $progressArtifact = [ordered]@{
        schema_version = 1
        status = 'passed'
        invocation = $invocationTag
        batch_number = $batchNumber
        batch_size = $BatchSize
        completed_before = [int]$progressResult.progress.stage_completed_runs
        completed_after = [int]$after.progress.stage_completed_runs
        remaining_after = [int]$after.progress.stage_remaining_runs
        source_tree_sha256 = $after.execution_provenance.source_tree_sha256
        run_report = if ($null -eq $runReport) { $null } else { $runReport.Substring($repoRoot.Length + 1).Replace('\', '/') }
    }
    $progressArtifact | ConvertTo-Json -Depth 4 | Out-File -LiteralPath $progressReport -Encoding utf8
    Write-Host ("PASS batch/progress: completed={0}/480, remaining={1}" -f `
        $after.progress.stage_completed_runs,
        $after.progress.stage_remaining_runs)
    if ([int]$after.progress.stage_remaining_runs -gt 0) {
        Write-Host 'Step 7 is not yet complete. Run this same command again for the next incomplete batch.'
        return
    }

    $finalFiles = @(
        $profiles,
        $profileRuns,
        $profileSummary,
        (Join-Path $plotRoot 'sensitivity-curves.png'),
        (Join-Path $plotRoot 'intensity-heatmap.png'),
        (Join-Path $plotRoot 'sensitivity-intensity.png')
    )
    $existingFinal = @($finalFiles | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    if ($existingFinal.Count -eq 0) {
        Write-Host '[Step 7] Building 480-row JSONL, 160-row Parquet, 32 curves and three plots...'
        $buildRaw = python -m gaugur_lite features build-profiles `
            --plan $plan `
            --baseline-plan $baselinePlan `
            --solo-baselines $soloBaselines `
            --calibration $calibration `
            --out $profiles `
            --runs-out $profileRuns `
            --summary $profileSummary `
            --plot-dir $plotRoot
        if ($LASTEXITCODE -ne 0) {
            $buildRaw | Out-Host
            throw "Profile build failed with exit code $LASTEXITCODE"
        }
        $buildResult = ($buildRaw | Out-String) | ConvertFrom-Json
        Write-Host ("PASS profiles: runs={0}, aggregate cells={1}, curves={2}, max zero deviation={3:N5}" -f `
            $buildResult.run_count,
            $buildResult.aggregate_cell_count,
            $buildResult.curve_count,
            [double]$buildResult.max_pressure_zero_retention_abs_deviation)
    }
    elseif ($existingFinal.Count -ne $finalFiles.Count) {
        throw "Partial Step 7 final outputs exist; stop for audit instead of overwriting: $($existingFinal -join ', ')"
    }

    Write-Host '[Step 7] Independently recomputing profiles and verifying Parquet/JSONL/PNG hashes...'
    $verifyRaw = python -m gaugur_lite features verify-profiles `
        --plan $plan `
        --baseline-plan $baselinePlan `
        --solo-baselines $soloBaselines `
        --calibration $calibration `
        --profiles $profiles `
        --runs $profileRuns `
        --summary $profileSummary `
        --plot-dir $plotRoot `
        --output $profileVerification
    if ($LASTEXITCODE -ne 0) {
        $verifyRaw | Out-Host
        throw "Profile independent verification failed with exit code $LASTEXITCODE"
    }
    $verifyResult = ($verifyRaw | Out-String) | ConvertFrom-Json
    Write-Host ("PASS independent verification: {0}/{1} checks, summary SHA-256={2}" -f `
        $verifyResult.passed_count,
        $verifyResult.check_count,
        $verifyResult.summary_sha256)
    Write-Host "Step 7 acceptance artifacts written to: $artifactRoot"
}
finally {
    Pop-Location
}
