[CmdletBinding()]
param(
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
$config = Join-Path $repoRoot 'configs\local.safety-v2-s30.yaml'
$plan = Join-Path $repoRoot 'artifacts\plans\formal-v1-safety-v2-s30.csv'
$baselinePlan = Join-Path $repoRoot 'artifacts\plans\formal-v1.csv'
$solo = Join-Path $repoRoot 'data\interim\formal-v1\solo-baselines.json'
$root = Join-Path $repoRoot 'artifacts\calibration\step7-safety-v2'
$calibration = Join-Path $root 'formal-calibration-stable-v1.json'
$metrics = Join-Path $root 'formal-calibration-stable-v1-metrics.jsonl'
$status = Join-Path $root 'formal-calibration-stable-v1-status.json'
$workers = Join-Path $root 'formal-calibration-stable-v1-workers'
$plot = Join-Path $root 'pressure-calibration-stable-v1.png'
$verification = Join-Path $root 'formal-calibration-stable-v1-verification.json'
$acceptance = Join-Path $root 'formal-calibration-stable-v1-acceptance.json'
$dryCalibration = Join-Path $root 'candidate-003-dry-run-only.json'
$dryPlot = Join-Path $root 'candidate-003-dry-run-only.png'
$failedAudit = Join-Path $root 'rejected-candidate-002-confirmation-audit.json'
$idleTemperatureAmendment = Join-Path $repoRoot 'artifacts\profiles\step7\safety-v2-idle-temperature-amendment.json'
$protocol = 'native_threads_1_warmup5_duration15_repeats5_v1'

function Get-DirectoryTreeSha256 {
    param([Parameter(Mandatory = $true)][string]$Directory)

    $rootPath = [System.IO.Path]::GetFullPath($Directory).TrimEnd('\')
    $files = @(Get-ChildItem -LiteralPath $rootPath -Recurse -File | Sort-Object FullName)
    $lines = foreach ($file in $files) {
        if (-not $file.FullName.StartsWith("$rootPath\", [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Worker artifact escaped the expected root: $($file.FullName)"
        }
        $relative = $file.FullName.Substring($rootPath.Length + 1).Replace('\', '/')
        $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "$relative`t$hash"
    }
    $payload = if ($lines.Count -gt 0) { ($lines -join "`n") + "`n" } else { '' }
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha256.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($payload))
    }
    finally {
        $sha256.Dispose()
    }
    return [pscustomobject]@{
        file_count = $files.Count
        sha256 = ([System.BitConverter]::ToString($digest)).Replace('-', '').ToLowerInvariant()
    }
}

Push-Location $repoRoot
try {
    $required = @($config, $plan, $baselinePlan, $solo, $failedAudit, $idleTemperatureAmendment)
    $missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
    if ($missing.Count -gt 0) {
        throw "Missing Candidate 003 inputs: $($missing -join ', ')"
    }
    $rejection = Get-Content -LiteralPath $failedAudit -Raw | ConvertFrom-Json
    if ($rejection.status -ne 'rejected' `
            -or $rejection.decision.selective_retry_allowed -ne $false `
            -or $rejection.decision.fresh_protocol_required -ne $true `
            -or @($rejection.checks | Where-Object { $_.passed -ne $true }).Count -ne 0) {
        throw 'Candidate 002 rejection audit is not complete.'
    }
    $temperatureProtocol = Get-Content -LiteralPath $idleTemperatureAmendment -Raw | ConvertFrom-Json
    if ([int]$temperatureProtocol.revised_batch_start_gpu_temp_max_c -ne 55 `
            -or [int]$temperatureProtocol.unchanged_max_gpu_temp_c -ne 80) {
        throw 'Unexpected idle-temperature amendment.'
    }

    $dryRaw = python -m gaugur_lite benchmark calibrate `
        --config $config `
        --resources cpu_compute,memory_bandwidth,gpu_compute,gpu_memory `
        --levels 0,0.25,0.5,0.75,1.0 `
        --repeats 5 `
        --warmup-s 5 `
        --duration-s 15 `
        --sample-interval-s 1 `
        --gpu-index 0 `
        --cpu-workers 8 `
        --memory-buffer-mib 64 `
        --gpu-matrix-size 1024 `
        --gpu-memory-max-mib 1024 `
        --benchmark-protocol $protocol `
        --output $dryCalibration `
        --plot $dryPlot `
        --dry-run
    if ($LASTEXITCODE -ne 0) {
        $dryRaw | Out-Host
        throw "Candidate 003 dry-run failed with exit code $LASTEXITCODE"
    }
    $dry = ($dryRaw | Out-String) | ConvertFrom-Json
    if ([int]$dry.cell_count -ne 100 `
            -or [int]$dry.repeats -ne 5 `
            -or [double]$dry.warmup_s -ne 5 `
            -or [double]$dry.duration_s -ne 15 `
            -or $dry.benchmark_protocol -ne $protocol) {
        throw 'Candidate 003 dry-run contract mismatch.'
    }
    if ($PreflightOnly) {
        [ordered]@{
            status = 'passed'
            candidate = 3
            cell_count = 100
            benchmark_protocol = $protocol
            warmup_s = 5
            duration_s = 15
            repeats = 5
            max_gpu_temp_c = 80
            start_gpu_temp_max_c = 55
            estimated_minutes = [Math]::Ceiling([double]$dry.estimated_measurement_s / 60)
            mutations_planned = $dry.mutations_planned
        } | ConvertTo-Json -Depth 4
        return
    }

    $forbiddenChanges = @(
        git status --short -- README.md gaugur_lite configs pyproject.toml scripts tests
    )
    if ($forbiddenChanges.Count -gt 0) {
        throw "Source/config changes are forbidden during Candidate 003:`n$($forbiddenChanges -join "`n")"
    }
    $candidateOutputs = @($calibration, $metrics, $status, $workers, $plot)
    $existing = @($candidateOutputs | Where-Object { Test-Path -LiteralPath $_ })
    if ($existing.Count -gt 0 -and $existing.Count -ne $candidateOutputs.Count) {
        throw "Partial Candidate 003 outputs exist; preserve and audit them: $($existing -join ', ')"
    }
    if ($existing.Count -eq 0) {
        $allChanges = @(git status --short)
        if ($allChanges.Count -gt 0) {
            throw "Worktree must be fully clean before Candidate 003 starts:`n$($allChanges -join "`n")"
        }
        $gpuTemp = [int](
            nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits |
                Select-Object -First 1
        )
        Write-Host "[Candidate 003] Start GPU temperature: $gpuTemp C"
        if ($gpuTemp -gt 55) {
            throw 'GPU must cool to 55 C or below before Candidate 003.'
        }
        Write-Host '[Candidate 003] Running unit tests before any benchmark worker...'
        python -m pytest tests\unit -q -p no:cacheprovider `
            --basetemp .test-tmp\step7-candidate003-unit
        if ($LASTEXITCODE -ne 0) {
            throw "Unit tests failed with exit code $LASTEXITCODE; calibration did not start."
        }
        Write-Host '[Candidate 003] Running 100 stable calibration cells (~35 minutes)...'
        python -m gaugur_lite benchmark calibrate `
            --config $config `
            --resources cpu_compute,memory_bandwidth,gpu_compute,gpu_memory `
            --levels 0,0.25,0.5,0.75,1.0 `
            --repeats 5 `
            --warmup-s 5 `
            --duration-s 15 `
            --sample-interval-s 1 `
            --gpu-index 0 `
            --cpu-workers 8 `
            --memory-buffer-mib 64 `
            --gpu-matrix-size 1024 `
            --gpu-memory-max-mib 1024 `
            --benchmark-protocol $protocol `
            --output $calibration `
            --plot $plot | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Candidate 003 calibration failed with exit code $LASTEXITCODE"
        }
    }

    Write-Host '[Candidate 003] Independently verifying JSONL, workers, provenance and quality gates...'
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
        throw "Candidate 003 verification failed with exit code $LASTEXITCODE"
    }
    $verified = ($verifyRaw | Out-String) | ConvertFrom-Json

    Write-Host '[Candidate 003] Applying the unchanged 5% denominator gate...'
    $auditRaw = python -m gaugur_lite features build-profiles `
        --plan $plan `
        --baseline-plan $baselinePlan `
        --solo-baselines $solo `
        --calibration $calibration `
        --out data\interim\formal-v1\safety-v2\profiles.parquet `
        --runs-out data\interim\formal-v1\safety-v2\profile-runs.jsonl `
        --summary data\interim\formal-v1\safety-v2\profile-summary.json `
        --plot-dir artifacts\profiles\step7\safety-v2\plots `
        --dry-run
    if ($LASTEXITCODE -ne 0) {
        $auditRaw | Out-Host
        throw "Candidate 003 denominator gate failed with exit code $LASTEXITCODE; do not rerun."
    }
    $audit = ($auditRaw | Out-String) | ConvertFrom-Json
    if ($audit.calibration_benchmark_protocol -ne $protocol `
            -or [int]$audit.standalone_repeat_count -ne 5 `
            -or $null -ne $audit.calibration_confirmation `
            -or [double]$audit.standalone_throughput_cv_max_pct -gt 5) {
        throw 'Candidate 003 profile-input audit returned an unexpected contract.'
    }

    $workerTree = Get-DirectoryTreeSha256 -Directory $workers
    if ([int]$workerTree.file_count -ne 400) {
        throw "Candidate 003 worker tree must contain exactly 400 files; found $($workerTree.file_count)."
    }

    $acceptancePayload = [ordered]@{
        schema_version = 1
        status = 'passed'
        candidate = 3
        benchmark_protocol = $protocol
        cell_count = 100
        denominator_repeat_count = 5
        denominator_nonzero_cell_count = [int]$audit.standalone_nonzero_cell_count
        denominator_cv_threshold_pct = 5.0
        denominator_cv_max_pct = [double]$audit.standalone_throughput_cv_max_pct
        calibration = 'artifacts/calibration/step7-safety-v2/formal-calibration-stable-v1.json'
        calibration_sha256 = (Get-FileHash -LiteralPath $calibration -Algorithm SHA256).Hash.ToLowerInvariant()
        metrics_sha256 = (Get-FileHash -LiteralPath $metrics -Algorithm SHA256).Hash.ToLowerInvariant()
        status_sha256 = (Get-FileHash -LiteralPath $status -Algorithm SHA256).Hash.ToLowerInvariant()
        worker_file_count = [int]$workerTree.file_count
        worker_tree_sha256 = [string]$workerTree.sha256
        verification_sha256 = (Get-FileHash -LiteralPath $verification -Algorithm SHA256).Hash.ToLowerInvariant()
        plot_sha256 = (Get-FileHash -LiteralPath $plot -Algorithm SHA256).Hash.ToLowerInvariant()
        verification_check_count = @($verified.checks).Count
        base_confirmation_reused = $false
    }
    $encoded = ($acceptancePayload | ConvertTo-Json -Depth 5) + "`n"
    if (Test-Path -LiteralPath $acceptance -PathType Leaf) {
        $stored = Get-Content -LiteralPath $acceptance -Raw
        if ($stored -ne $encoded) {
            throw 'Existing Candidate 003 acceptance differs; refusing overwrite.'
        }
    }
    else {
        [System.IO.File]::WriteAllText(
            $acceptance,
            $encoded,
            [System.Text.UTF8Encoding]::new($false)
        )
    }
    Write-Host ("PASS Candidate 003: cells=100, max denominator CV={0:N4}%" -f `
        [double]$audit.standalone_throughput_cv_max_pct)
}
finally {
    Pop-Location
}
