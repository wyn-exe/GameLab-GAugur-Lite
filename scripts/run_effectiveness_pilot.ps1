[CmdletBinding()]
param(
    [string]$BasePlan = 'artifacts\plans\formal-v1-safety-v2-s30.csv',
    [string]$LocalConfig = 'configs\local.safety-v2-s30.yaml',
    [string]$StressPlan = 'artifacts\plans\formal-effectiveness-v1-cpu-p100.csv',
    [string]$SoloBaselines = 'data\interim\formal-v1\solo-baselines.json',
    [string]$PilotDirectory = 'artifacts\effectiveness\pilot',
    [int]$BatchSize = 12,
    [int]$BenchmarkCpuWorkers = 64,
    [double]$QosRatio = 0.80
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($BatchSize -lt 1 -or $BatchSize -gt 36) { throw 'BatchSize must be in [1, 36].' }
if ($BenchmarkCpuWorkers -lt 32 -or $BenchmarkCpuWorkers -gt 64) { throw 'BenchmarkCpuWorkers must be in [32, 64].' }

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$basePlanPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $BasePlan))
$localConfigPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $LocalConfig))
$stressPlanPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $StressPlan))
$soloPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $SoloBaselines))
$pilotPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $PilotDirectory))
$pilotAcceptance = Join-Path $pilotPath 'stress-pilot-acceptance.json'
$invocationRoot = Join-Path $pilotPath 'invocations'

Push-Location $repoRoot
try {
    $required = @(
        $basePlanPath,
        (Join-Path $repoRoot 'artifacts\plans\formal-v1-safety-v2-s30-manifest.json'),
        $localConfigPath,
        $soloPath
    )
    $missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
    if ($missing.Count -gt 0) { throw "Missing effectiveness pilot inputs: $($missing -join ', ')" }
    if (Test-Path -LiteralPath $pilotAcceptance -PathType Leaf) {
        throw "Stress pilot acceptance already exists; do not overwrite: $pilotAcceptance"
    }
    $sourceChanges = @(git status --short -- README.md gaugur_lite configs pyproject.toml scripts tests)
    if ($sourceChanges.Count -gt 0) {
        throw "Commit effectiveness correction source/config changes before pilot:`n$($sourceChanges -join "`n")"
    }

    New-Item -ItemType Directory -Force -Path $invocationRoot | Out-Null
    $invocationNumber = 1
    do {
        $invocationLabel = 'invocation-{0:D3}' -f $invocationNumber
        $unitLog = Join-Path $invocationRoot "$invocationLabel-unit-tests.txt"
        $invocationNumber++
    } while (Test-Path -LiteralPath $unitLog)
    Write-Host '[Effectiveness pilot] Running unit tests before stressed co-location...'
    $unitOutput = python -m pytest tests\unit -q -p no:cacheprovider `
        --basetemp '.test-tmp\effectiveness-pilot-unit' 2>&1
    $unitSucceeded = $?
    $unitOutput | Out-File -LiteralPath $unitLog -Encoding utf8
    $unitOutput | Out-Host
    if (-not $unitSucceeded) { throw 'Unit tests failed; no stressed co-location was started.' }

    $stressFiles = @(
        $stressPlanPath,
        ($stressPlanPath -replace '\.csv$', '-manifest.json'),
        ($stressPlanPath -replace '\.csv$', '-combinations.json')
    )
    $existingStress = @($stressFiles | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    if ($existingStress.Count -ne 0 -and $existingStress.Count -ne $stressFiles.Count) {
        throw "Partial stress plan exists; stop for audit: $($existingStress -join ', ')"
    }
    if ($existingStress.Count -eq 0) {
        Write-Host '[Effectiveness pilot] Generating a CPU-p100 stress plan from the frozen 216-run shape...'
        python -m gaugur_lite effectiveness plan-stress `
            --base-plan $basePlanPath `
            --local-config $localConfigPath `
            --out $stressPlanPath `
            --experiment-id formal-effectiveness-v1 `
            --resource cpu_compute `
            --pressure 1.0 `
            --cpu-workers $BenchmarkCpuWorkers `
            --raw-root data/raw/formal-effectiveness-v1 | Out-Host
        if (-not $?) { throw 'Stress plan generation failed.' }
    }

    $progressRaw = python -m gaugur_lite run --plan $stressPlanPath --stage colocation-main --resume --dry-run
    if (-not $?) { $progressRaw | Out-Host; throw 'Stress runner preflight failed.' }
    $progress = ($progressRaw | Out-String | ConvertFrom-Json)
    while ([int]$progress.progress.stage_completed_runs -lt $BatchSize) {
        $firstPending = -1
        for ($index = 0; $index -lt $progress.decisions.Count; $index++) {
            if ($progress.decisions[$index].action -eq 'run') { $firstPending = $index; break }
        }
        if ($firstPending -lt 0) { throw 'Runner reports pending stress runs but no pending decision exists.' }
        $batchNumber = [int][Math]::Floor($firstPending / $BatchSize) + 1
        $runReport = Join-Path $invocationRoot ('{0}-colocation-main-batch-{1:D3}-run.json' -f $invocationLabel, $batchNumber)
        Write-Host "[Effectiveness pilot] Running CPU-p100 batch $batchNumber; completed=$($progress.progress.stage_completed_runs)/$BatchSize; workers=$BenchmarkCpuWorkers"
        $env:GAUGUR_BENCHMARK_CPU_WORKERS = [string]$BenchmarkCpuWorkers
        python -m gaugur_lite run `
            --plan $stressPlanPath `
            --stage colocation-main `
            --resume `
            --batch-number $batchNumber `
            --batch-size $BatchSize `
            --fail-fast `
            --report $runReport | Out-Host
        if (-not $?) { throw "Stress batch $batchNumber failed; artifacts preserved." }
        $progressRaw = python -m gaugur_lite run --plan $stressPlanPath --stage colocation-main --resume --dry-run
        if (-not $?) { throw 'Stress runner progress check failed.' }
        $progress = ($progressRaw | Out-String | ConvertFrom-Json)
    }

    Write-Host '[Effectiveness pilot] Auditing measured retention for non-degenerate QoS labels...'
    $auditOutput = Join-Path $pilotPath 'stress-pilot-acceptance.json'
    $auditRaw = python -m gaugur_lite effectiveness audit-pilot `
        --plan $stressPlanPath `
        --solo-baselines $soloPath `
        --qos-ratio $QosRatio `
        --min-completed-runs $BatchSize `
        --min-positive-targets 4 `
        --min-negative-targets 4 `
        --benchmark-cpu-workers $BenchmarkCpuWorkers `
        --output $auditOutput 2>&1
    $auditSucceeded = $?
    $auditRaw | Out-Host
    if (-not $auditSucceeded) {
        throw 'Stress pilot did not produce non-degenerate QoS labels; do not start full collection.'
    }
    Write-Host "PASS effectiveness pilot: $pilotPath"
    Write-Host '[Effectiveness pilot] Pilot only completed. Full 216-run truth collection is intentionally not started.'
}
finally {
    Pop-Location
}
