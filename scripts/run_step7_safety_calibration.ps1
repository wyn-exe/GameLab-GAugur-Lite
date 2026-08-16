[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $repoRoot
}
else {
    "$repoRoot$([System.IO.Path]::PathSeparator)$env:PYTHONPATH"
}
$config = Join-Path $repoRoot 'configs\local.safety-v2-s30.yaml'
$plan = Join-Path $repoRoot 'artifacts\plans\formal-v1-safety-v2-s30.csv'
$baselinePlan = Join-Path $repoRoot 'artifacts\plans\formal-v1.csv'
$solo = Join-Path $repoRoot 'data\interim\formal-v1\solo-baselines.json'
$root = Join-Path $repoRoot 'artifacts\calibration\step7-safety-v2'
$calibration = Join-Path $root 'formal-calibration-warmup-v1.json'
$verification = Join-Path $root 'formal-calibration-warmup-v1-verification.json'
$plot = Join-Path $root 'pressure-calibration-warmup-v1.png'

Push-Location $repoRoot
try {
    if (-not (Test-Path -LiteralPath $plan -PathType Leaf)) {
        throw 'Missing frozen safety-v2 plan. Run prepare_step7_safety_v2.ps1 and commit it first.'
    }
    if ((git status --porcelain=v1 --untracked-files=normal | Measure-Object).Count -ne 0) {
        throw 'The worktree must be clean before safety calibration.'
    }
    $gpuTemp = [int](
        nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits |
            Select-Object -First 1
    )
    Write-Host "[Safety-v2 calibration] Start GPU temperature: $gpuTemp C"
    if ($gpuTemp -gt 50) {
        throw 'GPU must cool to 50 C or below before calibration.'
    }

    if (-not (Test-Path -LiteralPath $calibration -PathType Leaf)) {
        Write-Host '[Safety-v2 calibration] Running 60 capped calibration cells (about 8 minutes)...'
        python -m gaugur_lite benchmark calibrate `
            --config $config `
            --resources cpu_compute,memory_bandwidth,gpu_compute,gpu_memory `
            --levels 0,0.25,0.5,0.75,1.0 `
            --repeats 3 `
            --warmup-s 1 `
            --duration-s 6 `
            --sample-interval-s 1 `
            --gpu-index 0 `
            --cpu-workers 8 `
            --memory-buffer-mib 64 `
            --gpu-matrix-size 1024 `
            --gpu-memory-max-mib 1024 `
            --output $calibration `
            --plot $plot | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Safety calibration failed with exit code $LASTEXITCODE"
        }
    }

    Write-Host '[Safety-v2 calibration] Verifying JSONL hash and quality gates...'
    if (Test-Path -LiteralPath $verification -PathType Leaf) {
        $verifyRaw = python -m gaugur_lite benchmark verify --calibration $calibration
    }
    else {
        $verifyRaw = python -m gaugur_lite benchmark verify `
            --calibration $calibration `
            --output $verification
    }
    if ($LASTEXITCODE -ne 0) {
        $verifyRaw | Out-Host
        throw "Safety calibration verification failed with exit code $LASTEXITCODE"
    }

    Write-Host '[Safety-v2 calibration] Auditing baseline and capped denominator compatibility...'
    $auditRaw = python -m gaugur_lite features build-profiles `
        --plan $plan `
        --baseline-plan $baselinePlan `
        --solo-baselines $solo `
        --calibration $calibration `
        --out data\interim\formal-v1\profiles.parquet `
        --runs-out data\interim\formal-v1\profile-runs.jsonl `
        --summary data\interim\formal-v1\profile-summary.json `
        --plot-dir artifacts\profiles\step7\safety-v2\plots `
        --dry-run
    if ($LASTEXITCODE -ne 0) {
        $auditRaw | Out-Host
        throw "Safety-v2 input audit failed with exit code $LASTEXITCODE"
    }
    $audit = ($auditRaw | Out-String) | ConvertFrom-Json
    if ($audit.baseline_contract -ne 'safety_v2_capped_gpu_compute' `
            -or [double]$audit.pressure_caps.gpu_compute -ne 0.25 `
            -or [double]$audit.gpu_temperature_max_c -ne 80) {
        throw 'Safety-v2 profile audit did not activate the expected contract.'
    }
    Write-Host ("PASS safety calibration: cells=60, nonzero denominators={0}, max CV={1:N4}%" -f `
        $audit.standalone_nonzero_cell_count,
        [double]$audit.standalone_throughput_cv_max_pct)
    Write-Host "Artifacts written to: $root"
}
finally {
    Pop-Location
}
