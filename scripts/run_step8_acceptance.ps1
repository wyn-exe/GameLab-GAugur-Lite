[CmdletBinding()]
param(
    [ValidateRange(1, 36)]
    [int]$BatchSize = 12,
    [switch]$PreflightOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $repoRoot
}
else {
    "$repoRoot$([System.IO.Path]::PathSeparator)$env:PYTHONPATH"
}

$plan = Join-Path $repoRoot 'artifacts\plans\formal-v1-safety-v2-s30.csv'
$planManifest = Join-Path $repoRoot 'artifacts\plans\formal-v1-safety-v2-s30-manifest.json'
$planCombinations = Join-Path $repoRoot 'artifacts\plans\formal-v1-safety-v2-s30-combinations.json'
$planVerification = Join-Path $repoRoot 'artifacts\plans\formal-v1-safety-v2-s30-verification.json'
$solo = Join-Path $repoRoot 'data\interim\formal-v1\solo-baselines.json'
$step7Summary = Join-Path $repoRoot 'data\interim\formal-v1\safety-v2\profile-summary.json'
$step7Verification = Join-Path $repoRoot 'artifacts\profiles\step7\safety-v2\formal-profile-verification.json'
$artifactRoot = Join-Path $repoRoot 'artifacts\colocation\step8\safety-v2'
$invocationRoot = Join-Path $artifactRoot 'invocations'
$plot = Join-Path $artifactRoot 'plots\retention-by-size.png'
$runs = Join-Path $repoRoot 'data\interim\formal-v1\safety-v2\colocation-runs.jsonl'
$truth = Join-Path $repoRoot 'data\interim\formal-v1\safety-v2\colocation-truth.parquet'
$summary = Join-Path $repoRoot 'data\interim\formal-v1\safety-v2\colocation-summary.json'
$verification = Join-Path $artifactRoot 'formal-colocation-verification.json'
$acceptance = Join-Path $artifactRoot 'formal-colocation-acceptance.json'

function Get-GpuTemperature {
    $raw = @(nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits)
    if (-not $? -or $raw.Count -lt 1) {
        throw 'nvidia-smi temperature query failed.'
    }
    return [int]$raw[0].Trim()
}

function Wait-GpuCool {
    param(
        [int]$TargetC = 55,
        [int]$TimeoutSeconds = 1800
    )
    $started = Get-Date
    $samples = @()
    while ($true) {
        $temperature = Get-GpuTemperature
        $samples += [ordered]@{
            wall_time = (Get-Date).ToString('o')
            gpu_temp_c = $temperature
        }
        Write-Host "[Step 8] GPU temperature: $temperature C (batch start requires <= $TargetC C)"
        if ($temperature -le $TargetC) {
            return $samples
        }
        if (((Get-Date) - $started).TotalSeconds -ge $TimeoutSeconds) {
            throw "GPU did not cool to $TargetC C within $TimeoutSeconds seconds."
        }
        Start-Sleep -Seconds 15
    }
}

function Get-RunnerProgress {
    param([string]$Stage)
    $raw = python -m gaugur_lite run --plan $plan --stage $Stage --resume --dry-run
    if (-not $?) {
        $raw | Out-Host
        throw "Runner preflight failed for stage=$Stage"
    }
    return ($raw | Out-String | ConvertFrom-Json)
}

function Invoke-ColocationStage {
    param(
        [string]$Stage,
        [int]$ExpectedRuns,
        [string]$InvocationTag
    )
    $progress = Get-RunnerProgress -Stage $Stage
    if ([int]$progress.progress.stage_total_runs -ne $ExpectedRuns) {
        throw "Unexpected stage size for ${Stage}: $($progress.progress.stage_total_runs)/$ExpectedRuns"
    }
    while ([int]$progress.progress.stage_remaining_runs -gt 0) {
        $firstPending = -1
        for ($index = 0; $index -lt $progress.decisions.Count; $index++) {
            if ($progress.decisions[$index].action -eq 'run') {
                $firstPending = $index
                break
            }
        }
        if ($firstPending -lt 0) {
            throw "stage=$Stage reports remaining runs but no pending decision exists."
        }
        $batchNumber = [int][Math]::Floor($firstPending / $BatchSize) + 1
        $cooldownLog = Join-Path $invocationRoot ("{0}-{1}-batch-{2:D3}-cooldown.json" -f $InvocationTag, $Stage, $batchNumber)
        $runReport = Join-Path $invocationRoot ("{0}-{1}-batch-{2:D3}-run.json" -f $InvocationTag, $Stage, $batchNumber)
        $samples = Wait-GpuCool -TargetC 55 -TimeoutSeconds 1800
        $samples | ConvertTo-Json -Depth 3 | Out-File -LiteralPath $cooldownLog -Encoding utf8

        Write-Host ("[Step 8] Running {0} batch {1}; remaining={2}/{3}; --fail-fast is enabled..." -f `
            $Stage, $batchNumber, $progress.progress.stage_remaining_runs, $ExpectedRuns)
        Write-Host '[Step 8] Keep all Pyxel windows visible and unobscured; do not minimize them.'
        $runRaw = python -m gaugur_lite run `
            --plan $plan `
            --stage $Stage `
            --resume `
            --batch-number $batchNumber `
            --batch-size $BatchSize `
            --fail-fast `
            --report $runReport
        if (-not $?) {
            $runRaw | Out-Host
            throw "A failed/invalid Step 8 attempt stopped stage=$Stage. Artifacts are preserved; do not auto-retry."
        }
        $runResult = ($runRaw | Out-String | ConvertFrom-Json)
        if ([int]$runResult.failed_or_invalid -ne 0 -or $runResult.stopped_early -eq $true) {
            throw "Stage=$Stage batch=$batchNumber did not finish cleanly."
        }
        $previousRemaining = [int]$progress.progress.stage_remaining_runs
        $progress = Get-RunnerProgress -Stage $Stage
        if ([int]$progress.progress.stage_remaining_runs -ge $previousRemaining) {
            throw "Stage=$Stage progress did not advance after batch=$batchNumber."
        }
        Write-Host ("PASS {0} progress: completed={1}/{2}, remaining={3}" -f `
            $Stage,
            $progress.progress.stage_completed_runs,
            $ExpectedRuns,
            $progress.progress.stage_remaining_runs)
    }
    return $progress
}

$required = @(
    $plan,
    $planManifest,
    $planCombinations,
    $solo,
    $step7Summary,
    $step7Verification
)
$missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
if ($missing.Count -gt 0) {
    throw "Missing Step 8 inputs: $($missing -join ', ')"
}

Push-Location $repoRoot
try {
    $manifest = Get-Content -LiteralPath $planManifest -Raw | ConvertFrom-Json
    $actualPlanHash = (Get-FileHash -LiteralPath $plan -Algorithm SHA256).Hash.ToLowerInvariant()
    if ([int]$manifest.row_count -ne 720 `
            -or $manifest.root_dirty_at_generation -ne $false `
            -or $actualPlanHash -ne [string]$manifest.plan_sha256 `
            -or [int]$manifest.stage_counts.'colocation-main' -ne 180 `
            -or [int]$manifest.stage_counts.'colocation-extra-test' -ne 36 `
            -or [double]$manifest.pressure_caps.gpu_compute -ne 0.25) {
        throw 'Unexpected frozen Safety-v2 plan contract for Step 8.'
    }
    # profile summary 含中文解释文本；让 Python 只输出 ASCII 的门槛字段，避免宿主控制台编码干扰 JSON 解析。
    $profileSummaryRaw = (& python -c 'import json,sys; p=json.load(open(sys.argv[1], encoding="utf-8")); print(json.dumps({"status":p.get("status"),"run_count":p.get("run_count"),"aggregate_cell_count":p.get("aggregate_cell_count")}, ensure_ascii=True))' $step7Summary | Out-String)
    if (-not $?) {
        throw 'Unable to parse Step 7 profile summary with Python.'
    }
    $profileSummary = ($profileSummaryRaw | Out-String | ConvertFrom-Json)
    $profileVerificationRaw = (& python -c 'import json,sys; p=json.load(open(sys.argv[1], encoding="utf-8")); print(json.dumps({"status":p.get("status"),"check_count":p.get("check_count"),"passed_count":p.get("passed_count")}, ensure_ascii=True))' $step7Verification | Out-String)
    if (-not $?) {
        throw 'Unable to parse Step 7 profile verification with Python.'
    }
    $profileVerification = ($profileVerificationRaw | Out-String | ConvertFrom-Json)
    if ($profileSummary.status -ne 'passed' `
            -or [int]$profileSummary.run_count -ne 480 `
            -or [int]$profileSummary.aggregate_cell_count -ne 160 `
            -or $profileVerification.status -ne 'passed' `
            -or [int]$profileVerification.passed_count -ne [int]$profileVerification.check_count) {
        throw 'Step 7 formal profiles must pass before starting Step 8.'
    }

    $auditRaw = python -m gaugur_lite features build-colocation `
        --plan $plan `
        --solo-baselines $solo `
        --runs-out $runs `
        --truth-out $truth `
        --summary $summary `
        --plot $plot `
        --dry-run
    if (-not $?) {
        $auditRaw | Out-Host
        throw 'Step 8 frozen-plan/baseline preflight failed.'
    }
    $audit = ($auditRaw | Out-String | ConvertFrom-Json)
    if ($audit.status -ne 'passed' `
            -or [int]$audit.main_physical_run_count -ne 180 `
            -or [int]$audit.extra_physical_run_count -ne 36 `
            -or [int]$audit.expected_main_target_count -ne 456 `
            -or [int]$audit.expected_extra_target_count -ne 144 `
            -or @($audit.checks.psobject.Properties | Where-Object { $_.Value -ne $true }).Count -ne 0) {
        throw 'Step 8 input audit did not satisfy the formal contract.'
    }
    $mainProgress = Get-RunnerProgress -Stage 'colocation-main'
    $extraProgress = Get-RunnerProgress -Stage 'colocation-extra-test'

    if ($PreflightOnly) {
        [ordered]@{
            status = 'passed'
            plan_sha256 = $actualPlanHash
            main_rows = 180
            extra_rows = 36
            expected_main_targets = 456
            expected_extra_targets = 144
            main_completed = [int]$mainProgress.progress.stage_completed_runs
            main_remaining = [int]$mainProgress.progress.stage_remaining_runs
            extra_completed = [int]$extraProgress.progress.stage_completed_runs
            extra_remaining = [int]$extraProgress.progress.stage_remaining_runs
            warmup_s = 10
            duration_s = 30
            cooldown_s = 10
            max_gpu_temp_c = 80
            batch_start_temp_c = 55
            batch_size = $BatchSize
            fail_fast = $true
        } | ConvertTo-Json
        return
    }

    $forbiddenChanges = @(
        git status --short -- README.md gaugur_lite configs pyproject.toml scripts tests
    )
    if ($forbiddenChanges.Count -gt 0) {
        throw "Commit all Step 8 source/config changes before formal collection:`n$($forbiddenChanges -join "`n")"
    }
    $finalFiles = @($runs, $truth, $summary, $plot)
    $existingFinal = @($finalFiles | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    # 仅允许“JSONL 已落盘、后续 truth 尚未写入”的可审计恢复，不覆盖其他最终产物。
    $recoverRunsOnly = $existingFinal.Count -eq 1 -and $existingFinal[0] -eq $runs
    if ($existingFinal.Count -ne 0 -and $existingFinal.Count -ne $finalFiles.Count -and -not $recoverRunsOnly) {
        throw "Partial Step 8 final outputs exist; stop for audit instead of overwriting: $($existingFinal -join ', ')"
    }

    New-Item -ItemType Directory -Force -Path $invocationRoot | Out-Null
    $invocationNumber = 1
    while (Test-Path -LiteralPath (Join-Path $invocationRoot ('invocation-{0:D3}-unit-tests.txt' -f $invocationNumber))) {
        $invocationNumber += 1
    }
    $invocationTag = 'invocation-{0:D3}' -f $invocationNumber
    $unitLog = Join-Path $invocationRoot "$invocationTag-unit-tests.txt"
    Write-Host "[Step 8/$invocationTag] Running unit tests before formal collection..."
    $unitOutput = python -m pytest tests\unit -q -p no:cacheprovider `
        --basetemp ".test-tmp\step8-$invocationTag-unit" 2>&1
    $unitSucceeded = $?
    $unitOutput | Out-File -LiteralPath $unitLog -Encoding utf8
    $unitOutput | Out-Host
    if (-not $unitSucceeded) {
        throw 'Unit tests failed; no formal batch was started.'
    }
    if (-not (Test-Path -LiteralPath $planVerification -PathType Leaf)) {
        python -m gaugur_lite plan-verify --plan $plan --output $planVerification | Out-Null
    }
    else {
        python -m gaugur_lite plan-verify --plan $plan | Out-Null
    }
    if (-not $?) {
        throw 'Frozen plan verification failed.'
    }

    $mainFinal = Invoke-ColocationStage -Stage 'colocation-main' -ExpectedRuns 180 -InvocationTag $invocationTag
    $extraFinal = Invoke-ColocationStage -Stage 'colocation-extra-test' -ExpectedRuns 36 -InvocationTag $invocationTag
    if ([int]$mainFinal.progress.stage_remaining_runs -ne 0 `
            -or [int]$extraFinal.progress.stage_remaining_runs -ne 0) {
        throw 'Step 8 runner did not finish all formal rows.'
    }

    if ($existingFinal.Count -eq 0 -or $recoverRunsOnly) {
        Write-Host '[Step 8] Building 216 physical records, 600 target truths and measured retention plot...'
        python -m gaugur_lite features build-colocation `
            --plan $plan `
            --solo-baselines $solo `
            --runs-out $runs `
            --truth-out $truth `
            --summary $summary `
            --plot $plot | Out-Host
        if (-not $?) {
            throw 'Step 8 truth build failed.'
        }
    }

    Write-Host '[Step 8] Independently recomputing all truth rows and artifact hashes...'
    if (-not (Test-Path -LiteralPath $verification -PathType Leaf)) {
        $verifyRaw = python -m gaugur_lite features verify-colocation `
            --plan $plan `
            --solo-baselines $solo `
            --runs $runs `
            --truth $truth `
            --summary $summary `
            --plot $plot `
            --output $verification
    }
    else {
        $verifyRaw = python -m gaugur_lite features verify-colocation `
            --plan $plan `
            --solo-baselines $solo `
            --runs $runs `
            --truth $truth `
            --summary $summary `
            --plot $plot
    }
    if (-not $?) {
        $verifyRaw | Out-Host
        throw 'Step 8 independent verification failed.'
    }
    $verified = ($verifyRaw | Out-String | ConvertFrom-Json)
    if ($verified.status -ne 'passed' `
            -or [int]$verified.passed_count -ne [int]$verified.check_count) {
        throw 'Step 8 independent verification returned failed checks.'
    }
    if (Test-Path -LiteralPath $acceptance -PathType Leaf) {
        throw "Step 8 acceptance artifact already exists; do not overwrite: $acceptance"
    }
    [ordered]@{
        schema_version = 1
        status = 'passed'
        plan_sha256 = $actualPlanHash
        main_physical_runs = 180
        extra_physical_runs = 36
        main_target_truths = 456
        extra_target_truths = 144
        verification_check_count = [int]$verified.check_count
        verification_passed_count = [int]$verified.passed_count
        runs = $runs.Substring($repoRoot.Length + 1).Replace('\', '/')
        truth = $truth.Substring($repoRoot.Length + 1).Replace('\', '/')
        summary = $summary.Substring($repoRoot.Length + 1).Replace('\', '/')
        plot = $plot.Substring($repoRoot.Length + 1).Replace('\', '/')
    } | ConvertTo-Json | Out-File -LiteralPath $acceptance -Encoding utf8
    Write-Host "PASS Step 8 formal acceptance: $artifactRoot"
}
finally {
    Pop-Location
}
