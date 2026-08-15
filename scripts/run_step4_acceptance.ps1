[CmdletBinding()]
param(
    [switch]$PreflightOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'README.md') -PathType Leaf)) {
    throw "Repository marker README.md not found under $repoRoot"
}

$artifactRoot = Join-Path $repoRoot 'artifacts\calibration\step4'
$calibrationOutput = Join-Path $artifactRoot 'formal-calibration.json'
$metricsOutput = Join-Path $artifactRoot 'formal-calibration-metrics.jsonl'
$statusOutput = Join-Path $artifactRoot 'formal-calibration-status.json'
$workersOutput = Join-Path $artifactRoot 'formal-calibration-workers'
$plotOutput = Join-Path $artifactRoot 'formal-calibration-curves.png'
$verificationOutput = Join-Path $artifactRoot 'formal-calibration-verification.json'
$resources = 'cpu_compute,memory_bandwidth,gpu_compute,gpu_memory'
$levels = '0,0.25,0.5,0.75,1.0'
$repeats = 3
$cellCount = 4 * 5 * $repeats

# Check every formal target before the first worker starts.
$plannedOutputs = @(
    $calibrationOutput,
    $metricsOutput,
    $statusOutput,
    $workersOutput,
    $plotOutput,
    $verificationOutput
)
$existing = @($plannedOutputs | Where-Object { Test-Path -LiteralPath $_ })
if ($existing.Count -gt 0) {
    throw "Formal Step 4 output already exists; do not overwrite: $($existing -join ', ')"
}

if ($PreflightOnly) {
    $preflightResult = [ordered]@{
        status = 'passed'
        powershell_version = $PSVersionTable.PSVersion.ToString()
        resources = $resources.Split(',')
        levels = $levels.Split(',')
        repeats = $repeats
        cell_count = $cellCount
        warmup_s = 1
        duration_s = 6
        sample_interval_s = 1
        estimated_measurement_s = $cellCount * 7
        formal_outputs_exist = $false
    }
    $preflightResult | ConvertTo-Json
    return
}

Push-Location $repoRoot
try {
    Write-Host '[Step 4] Running unit tests...'
    python -m pytest tests\unit -q -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) {
        throw "Unit tests failed with exit code $LASTEXITCODE"
    }

    Write-Host ("[Step 4] Starting {0} calibration cells (about 8 minutes including worker shutdown)..." -f $cellCount)
    $rawCalibration = python -m gaugur_lite benchmark calibrate `
        --config configs\local.example.yaml `
        --resources $resources `
        --levels $levels `
        --repeats $repeats `
        --warmup-s 1 `
        --duration-s 6 `
        --sample-interval-s 1 `
        --gpu-index 0 `
        --cpu-workers 8 `
        --memory-buffer-mib 64 `
        --gpu-matrix-size 1024 `
        --gpu-memory-max-mib 1024 `
        --output $calibrationOutput `
        --metrics-output $metricsOutput `
        --status-output $statusOutput `
        --workers-directory $workersOutput `
        --plot $plotOutput
    if ($LASTEXITCODE -ne 0) {
        $rawCalibration | Out-Host
        throw "Step 4 calibration failed with exit code $LASTEXITCODE; raw artifacts were preserved"
    }
    $calibration = ($rawCalibration | Out-String) | ConvertFrom-Json
    $calibration.resources | ForEach-Object {
        Write-Host ("PASS {0}: max abs error={1:N4}, actuation={2}" -f `
            $_.resource,
            [double]$_.max_abs_error,
            $_.actuation_kind)
    }

    Write-Host '[Step 4] Verifying calibration JSON, raw JSONL hash and quality gates...'
    $rawVerification = python -m gaugur_lite benchmark verify `
        --calibration $calibrationOutput `
        --output $verificationOutput
    if ($LASTEXITCODE -ne 0) {
        $rawVerification | Out-Host
        throw "Step 4 verification failed with exit code $LASTEXITCODE; artifacts were preserved"
    }
    $verification = ($rawVerification | Out-String) | ConvertFrom-Json
    Write-Host ("PASS verification: {0} checks, calibration SHA-256={1}" -f `
        $verification.checks.Count,
        $verification.calibration_sha256)
    Write-Host "Step 4 acceptance artifacts written to: $artifactRoot"
}
finally {
    Pop-Location
}
