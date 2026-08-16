[CmdletBinding()]
param(
    [switch]$DryRun
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
$base = Join-Path $repoRoot 'artifacts\calibration\step7-safety-v2\formal-calibration-warmup-v1.json'
$confirmation = Join-Path $repoRoot 'artifacts\calibration\step7-safety-v2\formal-calibration-confirmation-v1.json'
$plan = Join-Path $repoRoot 'artifacts\plans\formal-v1-safety-v2-s30.csv'
$baselinePlan = Join-Path $repoRoot 'artifacts\plans\formal-v1.csv'
$solo = Join-Path $repoRoot 'data\interim\formal-v1\solo-baselines.json'
$idleTemperatureAmendment = Join-Path $repoRoot 'artifacts\profiles\step7\safety-v2-idle-temperature-amendment.json'

Push-Location $repoRoot
try {
    if (-not (Test-Path -LiteralPath $base -PathType Leaf)) {
        throw 'Missing warmup-v1 base calibration.'
    }
    if (-not (Test-Path -LiteralPath $idleTemperatureAmendment -PathType Leaf)) {
        throw 'Missing Safety-v2 idle-temperature amendment.'
    }
    $temperatureProtocol = Get-Content -LiteralPath $idleTemperatureAmendment -Raw | ConvertFrom-Json
    if ($temperatureProtocol.status -ne 'sealed' `
            -or [int]$temperatureProtocol.previous_batch_start_gpu_temp_max_c -ne 50 `
            -or [int]$temperatureProtocol.revised_batch_start_gpu_temp_max_c -ne 55 `
            -or [int]$temperatureProtocol.unchanged_max_gpu_temp_c -ne 80) {
        throw 'Unexpected Safety-v2 idle-temperature amendment contract.'
    }
    $parentAmendment = Join-Path $repoRoot ([string]$temperatureProtocol.parent_safety_v2_amendment)
    $parentHash = (Get-FileHash -LiteralPath $parentAmendment -Algorithm SHA256).Hash.ToLowerInvariant()
    $baseHash = (Get-FileHash -LiteralPath $base -Algorithm SHA256).Hash.ToLowerInvariant()
    $planHash = (Get-FileHash -LiteralPath $plan -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($parentHash -ne [string]$temperatureProtocol.parent_safety_v2_amendment_sha256 `
            -or $baseHash -ne [string]$temperatureProtocol.base_calibration_sha256 `
            -or $planHash -ne [string]$temperatureProtocol.formal_plan_sha256) {
        throw 'Safety-v2 idle-temperature amendment hash binding failed.'
    }
    $startGpuTempMaxC = [int]$temperatureProtocol.revised_batch_start_gpu_temp_max_c
    if ($DryRun) {
        python scripts\run_step7_calibration_confirmation.py --dry-run
        exit $LASTEXITCODE
    }
    if ((git status --porcelain=v1 --untracked-files=normal | Measure-Object).Count -ne 0) {
        throw 'The worktree must be clean before calibration confirmation.'
    }
    $gpuTemp = [int](
        nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits |
            Select-Object -First 1
    )
    Write-Host "[Safety-v2 confirmation] Start GPU temperature: $gpuTemp C"
    if ($gpuTemp -gt $startGpuTempMaxC) {
        throw "GPU must cool to $startGpuTempMaxC C or below before confirmation."
    }

    Write-Host '[Safety-v2 confirmation] Running unit tests before six additional cells...'
    python -m pytest tests\unit -q -p no:cacheprovider `
        --basetemp .test-tmp\step7-confirmation-unit
    if ($LASTEXITCODE -ne 0) {
        throw "Unit tests failed with exit code $LASTEXITCODE; no confirmation cell started."
    }

    if (-not (Test-Path -LiteralPath $confirmation -PathType Leaf)) {
        Write-Host '[Safety-v2 confirmation] Running six deterministic r04/r05 cells (about 2 minutes)...'
        python scripts\run_step7_calibration_confirmation.py | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Calibration confirmation failed with exit code $LASTEXITCODE"
        }
    }

    Write-Host '[Safety-v2 confirmation] Auditing all five repeats with the unchanged 5% gate...'
    $auditRaw = python -m gaugur_lite features build-profiles `
        --plan $plan `
        --baseline-plan $baselinePlan `
        --solo-baselines $solo `
        --calibration $base `
        --calibration-confirmation $confirmation `
        --out data\interim\formal-v1\profiles.parquet `
        --runs-out data\interim\formal-v1\profile-runs.jsonl `
        --summary data\interim\formal-v1\profile-summary.json `
        --plot-dir artifacts\profiles\step7\safety-v2\plots `
        --dry-run
    if ($LASTEXITCODE -ne 0) {
        $auditRaw | Out-Host
        throw "Confirmed denominator audit failed with exit code $LASTEXITCODE"
    }
    $audit = ($auditRaw | Out-String) | ConvertFrom-Json
    if ([int]$audit.calibration_confirmation.selected_cell_count -ne 3 `
            -or [double]$audit.standalone_throughput_cv_max_pct -gt 5) {
        throw 'Confirmed denominator audit returned an unexpected contract.'
    }
    Write-Host ("PASS confirmation: selected=3, additional=6, max combined CV={0:N4}%" -f `
        [double]$audit.standalone_throughput_cv_max_pct)
    Write-Host "Artifacts written to: $(Split-Path -Parent $confirmation)"
}
finally {
    Pop-Location
}
