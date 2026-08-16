[CmdletBinding()]
param(
    [ValidateRange(1, 48)]
    [int]$BatchSize = 24,
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
$baselinePlan = Join-Path $repoRoot 'artifacts\plans\formal-v1.csv'
$solo = Join-Path $repoRoot 'data\interim\formal-v1\solo-baselines.json'
$calibration = Join-Path $repoRoot 'artifacts\calibration\step7-safety-v2\formal-calibration-warmup-v1.json'
$artifactRoot = Join-Path $repoRoot 'artifacts\profiles\step7\safety-v2'
$invocationRoot = Join-Path $artifactRoot 'invocations'
$plotRoot = Join-Path $artifactRoot 'plots'
$profileRoot = Join-Path $repoRoot 'data\interim\formal-v1\safety-v2'
$profiles = Join-Path $profileRoot 'profiles.parquet'
$profileRuns = Join-Path $profileRoot 'profile-runs.jsonl'
$profileSummary = Join-Path $profileRoot 'profile-summary.json'
$profileVerification = Join-Path $artifactRoot 'formal-profile-verification.json'
$planVerification = Join-Path $artifactRoot 'formal-plan-verification.json'

function Get-GpuTemperature {
    $raw = @(
        nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits
    )
    if ($LASTEXITCODE -ne 0 -or $raw.Count -lt 1) {
        throw 'nvidia-smi temperature query failed.'
    }
    return [int]$raw[0].Trim()
}

function Wait-GpuCool {
    param(
        [int]$TargetC = 50,
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
        Write-Host "[Safety-v2] GPU temperature: $temperature C (batch start requires <= $TargetC C)"
        if ($temperature -le $TargetC) {
            return $samples
        }
        if (((Get-Date) - $started).TotalSeconds -ge $TimeoutSeconds) {
            throw "GPU did not cool to $TargetC C within $TimeoutSeconds seconds."
        }
        Start-Sleep -Seconds 15
    }
}

$required = @($plan, $baselinePlan, $solo, $calibration)
$missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
if ($missing.Count -gt 0) {
    throw "Missing safety-v2 inputs: $($missing -join ', ')"
}

Push-Location $repoRoot
try {
    $forbiddenChanges = @(
        git status --short -- README.md gaugur_lite configs pyproject.toml scripts tests
    )
    if ($forbiddenChanges.Count -gt 0) {
        throw "Source/config changes are forbidden during formal collection:`n$($forbiddenChanges -join "`n")"
    }

    Write-Host '[Safety-v2] Auditing frozen plan, reused solo baseline and capped calibration...'
    $auditRaw = python -m gaugur_lite features build-profiles `
        --plan $plan `
        --baseline-plan $baselinePlan `
        --solo-baselines $solo `
        --calibration $calibration `
        --out $profiles `
        --runs-out $profileRuns `
        --summary $profileSummary `
        --plot-dir $plotRoot `
        --dry-run
    if ($LASTEXITCODE -ne 0) {
        $auditRaw | Out-Host
        throw "Safety-v2 input audit failed with exit code $LASTEXITCODE"
    }
    $audit = ($auditRaw | Out-String) | ConvertFrom-Json
    if ($audit.baseline_contract -ne 'safety_v2_capped_gpu_compute' `
            -or [double]$audit.gpu_temperature_max_c -ne 80 `
            -or [double]$audit.pressure_caps.gpu_compute -ne 0.25) {
        throw 'Unexpected safety-v2 audit contract.'
    }

    $progressRaw = python -m gaugur_lite run --plan $plan --stage profile --resume --dry-run
    if ($LASTEXITCODE -ne 0) {
        $progressRaw | Out-Host
        throw "Safety-v2 resume preflight failed with exit code $LASTEXITCODE"
    }
    $progress = ($progressRaw | Out-String) | ConvertFrom-Json
    if ($PreflightOnly) {
        [ordered]@{
            status = 'passed'
            profile_rows = 480
            completed = [int]$progress.progress.stage_completed_runs
            remaining = [int]$progress.progress.stage_remaining_runs
            batch_size = $BatchSize
            max_gpu_temp_c = 80
            cooldown_target_c = 70
            batch_start_temp_c = 50
            gpu_compute_cap = 0.25
            fail_fast = $true
        } | ConvertTo-Json -Depth 3
        return
    }

    New-Item -ItemType Directory -Force -Path $invocationRoot | Out-Null
    $invocationNumber = 1
    while (Test-Path -LiteralPath (Join-Path $invocationRoot ('invocation-{0:D3}-unit-tests.txt' -f $invocationNumber))) {
        $invocationNumber += 1
    }
    $invocationTag = 'invocation-{0:D3}' -f $invocationNumber
    $unitLog = Join-Path $invocationRoot "$invocationTag-unit-tests.txt"

    Write-Host "[Safety-v2/$invocationTag] Running unit tests once before all remaining batches..."
    $unitOutput = python -m pytest tests\unit -q -p no:cacheprovider `
        --basetemp ".test-tmp\safety-v2-$invocationTag-unit" 2>&1
    $unitExit = $LASTEXITCODE
    $unitOutput | Out-File -LiteralPath $unitLog -Encoding utf8
    $unitOutput | Out-Host
    if ($unitExit -ne 0) {
        throw "Unit tests failed with exit code $unitExit; no batch was started."
    }

    if (-not (Test-Path -LiteralPath $planVerification -PathType Leaf)) {
        python -m gaugur_lite plan-verify --plan $plan --output $planVerification | Out-Null
    }
    else {
        python -m gaugur_lite plan-verify --plan $plan | Out-Null
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Safety-v2 plan verification failed with exit code $LASTEXITCODE"
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
            throw 'Progress reports remaining rows but no pending decision exists.'
        }
        $batchNumber = [int][Math]::Floor($firstPending / $BatchSize) + 1
        $temperatureLog = Join-Path $invocationRoot ("{0}-batch-{1:D3}-cooldown.json" -f $invocationTag, $batchNumber)
        $samples = Wait-GpuCool -TargetC 50 -TimeoutSeconds 1800
        $samples | ConvertTo-Json -Depth 3 | Out-File -LiteralPath $temperatureLog -Encoding utf8

        $runReport = Join-Path $invocationRoot ("{0}-batch-{1:D3}-run.json" -f $invocationTag, $batchNumber)
        Write-Host ("[Safety-v2] Running batch {0}/{1}; --fail-fast is enabled..." -f `
            $batchNumber,
            [Math]::Ceiling(480 / $BatchSize))
        Write-Host '[Safety-v2] Keep the Pyxel window visible and unobscured.'
        $runRaw = python -m gaugur_lite run `
            --plan $plan `
            --stage profile `
            --resume `
            --batch-number $batchNumber `
            --batch-size $BatchSize `
            --fail-fast `
            --report $runReport
        $runExit = $LASTEXITCODE
        if ($runExit -ne 0) {
            $runRaw | Out-Host
            throw 'A failed/invalid attempt stopped the batch immediately. Artifacts are preserved; do not auto-retry.'
        }
        $runResult = ($runRaw | Out-String) | ConvertFrom-Json
        if ([int]$runResult.failed_or_invalid -ne 0 -or $runResult.stopped_early -eq $true) {
            throw 'Safety-v2 batch did not finish cleanly.'
        }

        $progressRaw = python -m gaugur_lite run --plan $plan --stage profile --resume --dry-run
        if ($LASTEXITCODE -ne 0) {
            $progressRaw | Out-Host
            throw "Post-batch progress failed with exit code $LASTEXITCODE"
        }
        $progress = ($progressRaw | Out-String) | ConvertFrom-Json
        Write-Host ("PASS progress: completed={0}/480, remaining={1}" -f `
            $progress.progress.stage_completed_runs,
            $progress.progress.stage_remaining_runs)
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
        Write-Host '[Safety-v2] Building 480 run records, 160 profiles, 32 curves and plots...'
        python -m gaugur_lite features build-profiles `
            --plan $plan `
            --baseline-plan $baselinePlan `
            --solo-baselines $solo `
            --calibration $calibration `
            --out $profiles `
            --runs-out $profileRuns `
            --summary $profileSummary `
            --plot-dir $plotRoot | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Profile build failed with exit code $LASTEXITCODE"
        }
    }
    elseif ($existingFinal.Count -ne $finalFiles.Count) {
        throw "Partial final outputs exist; stop for audit: $($existingFinal -join ', ')"
    }

    if (-not (Test-Path -LiteralPath $profileVerification -PathType Leaf)) {
        Write-Host '[Safety-v2] Independently recomputing profiles and hashes...'
        $verifyRaw = python -m gaugur_lite features verify-profiles `
            --plan $plan `
            --baseline-plan $baselinePlan `
            --solo-baselines $solo `
            --calibration $calibration `
            --profiles $profiles `
            --runs $profileRuns `
            --summary $profileSummary `
            --plot-dir $plotRoot `
            --output $profileVerification
        if ($LASTEXITCODE -ne 0) {
            $verifyRaw | Out-Host
            throw "Independent profile verification failed with exit code $LASTEXITCODE"
        }
    }
    Write-Host "PASS Step 7 safety-v2 acceptance: $artifactRoot"
}
finally {
    Pop-Location
}
