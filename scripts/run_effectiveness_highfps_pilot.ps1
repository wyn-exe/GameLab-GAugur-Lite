[CmdletBinding()]
param(
    [string]$BasePlan = 'artifacts\plans\formal-v1-safety-v2-s30.csv',
    [string]$LocalConfig = 'configs\local.safety-v2-s30.yaml',
    [string]$HighFpsPlan = 'artifacts\plans\formal-highfps-v1.csv',
    [string]$SoloBaselines = 'data\interim\formal-highfps-v1\solo-baselines.json',
    [string]$SoloRuns = 'data\interim\formal-highfps-v1\solo-runs.jsonl',
    [string]$SoloPlot = 'artifacts\effectiveness\highfps-pilot\solo-baselines.png',
    [string]$PilotDirectory = 'artifacts\effectiveness\highfps-pilot',
    [int]$BatchSize = 12,
    [double]$FpsMultiplier = 8.0,
    [double]$QosRatio = 0.80
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($BatchSize -lt 1 -or $BatchSize -gt 36) { throw 'BatchSize must be in [1, 36].' }
if ($FpsMultiplier -le 1.0 -or $FpsMultiplier -gt 16.0) { throw 'FpsMultiplier must be in (1, 16].' }
if ($QosRatio -le 0.0 -or $QosRatio -gt 1.0) { throw 'QosRatio must be in (0, 1].' }

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$basePlanPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $BasePlan))
$localConfigPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $LocalConfig))
$highFpsPlanPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $HighFpsPlan))
$soloPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $SoloBaselines))
$soloRunsPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $SoloRuns))
$soloPlotPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $SoloPlot))
$pilotPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $PilotDirectory))
$pilotAcceptance = Join-Path $pilotPath 'highfps-pilot-acceptance.json'
$invocationRoot = Join-Path $pilotPath 'invocations'

Push-Location $repoRoot
try {
    $required = @(
        $basePlanPath,
        (Join-Path $repoRoot 'artifacts\plans\formal-v1-safety-v2-s30-manifest.json'),
        $localConfigPath
    )
    $missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
    if ($missing.Count -gt 0) { throw "Missing high-FPS pilot inputs: $($missing -join ', ')" }
    if (Test-Path -LiteralPath $pilotAcceptance -PathType Leaf) {
        throw "High-FPS pilot acceptance already exists; do not overwrite: $pilotAcceptance"
    }

    $planFiles = @(
        $highFpsPlanPath,
        ($highFpsPlanPath -replace '\.csv$', '-manifest.json'),
        ($highFpsPlanPath -replace '\.csv$', '-combinations.json')
    )
    $existingPlan = @($planFiles | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    if ($existingPlan.Count -ne 0 -and $existingPlan.Count -ne $planFiles.Count) {
        throw "Partial high-FPS plan exists; stop for audit: $($existingPlan -join ', ')"
    }

    $sourceChanges = @(git status --short -- README.md gaugur_lite configs pyproject.toml scripts tests)
    if ($sourceChanges.Count -gt 0) {
        throw "Commit high-FPS correction source/config changes before pilot:`n$($sourceChanges -join "`n")"
    }

    New-Item -ItemType Directory -Force -Path $invocationRoot | Out-Null
    $invocationNumber = 1
    do {
        $invocationLabel = 'invocation-{0:D3}' -f $invocationNumber
        $unitLog = Join-Path $invocationRoot "$invocationLabel-unit-tests.txt"
        $invocationNumber++
    } while (Test-Path -LiteralPath $unitLog)

    Write-Host '[High-FPS pilot] Running unit tests before real-workload collection...'
    $unitOutput = python -m pytest tests\unit -q -p no:cacheprovider `
        --basetemp '.test-tmp\highfps-pilot-unit' 2>&1
    $unitSucceeded = $LASTEXITCODE -eq 0
    $unitOutput | Out-File -LiteralPath $unitLog -Encoding utf8
    $unitOutput | Out-Host
    if (-not $unitSucceeded) { throw 'Unit tests failed; no high-FPS workload was started.' }

    if ($existingPlan.Count -eq 0) {
        Write-Host "[High-FPS pilot] Generating a real-workload plan with fps multiplier $FpsMultiplier..."
        python -m gaugur_lite effectiveness plan-high-fps `
            --base-plan $basePlanPath `
            --local-config $localConfigPath `
            --out $highFpsPlanPath `
            --experiment-id formal-highfps-v1 `
            --fps-multiplier $FpsMultiplier `
            --raw-root data/raw/formal-highfps-v1 | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'High-FPS plan generation failed.' }
    }

    $env:GAUGUR_WORKLOAD_FPS_MULTIPLIER = [string]$FpsMultiplier
    # 先采集同倍率 solo，避免拿原生 FPS 分母解释高帧率共置结果。
    Write-Host '[High-FPS pilot] Running all 24 high-FPS solo baselines first...'
    $soloReport = Join-Path $invocationRoot "$invocationLabel-solo-run.json"
    python -m gaugur_lite run `
        --plan $highFpsPlanPath `
        --stage solo `
        --resume `
        --fail-fast `
        --report $soloReport | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'High-FPS solo baseline collection failed; artifacts preserved.' }

    $soloParent = Split-Path -Parent $soloPath
    New-Item -ItemType Directory -Force -Path $soloParent, (Split-Path -Parent $soloRunsPath), (Split-Path -Parent $soloPlotPath) | Out-Null
    Write-Host '[High-FPS pilot] Building high-FPS solo baselines...'
    python -m gaugur_lite summarize `
        --plan $highFpsPlanPath `
        --stage solo `
        --out $soloPath `
        --runs-out $soloRunsPath `
        --plot $soloPlotPath | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'High-FPS solo baseline summarization failed.' }

    $progressRaw = python -m gaugur_lite run --plan $highFpsPlanPath --stage colocation-main --resume --dry-run
    if ($LASTEXITCODE -ne 0) { $progressRaw | Out-Host; throw 'High-FPS runner preflight failed.' }
    $progress = ($progressRaw | Out-String | ConvertFrom-Json)
    # 只运行首批主共置；标签退化时由 audit 命令阻止全量采集。
    while ([int]$progress.progress.stage_completed_runs -lt $BatchSize) {
        $firstPending = -1
        for ($index = 0; $index -lt $progress.decisions.Count; $index++) {
            if ($progress.decisions[$index].action -eq 'run') { $firstPending = $index; break }
        }
        if ($firstPending -lt 0) { throw 'Runner reports pending high-FPS runs but no pending decision exists.' }
        $batchNumber = [int][Math]::Floor($firstPending / $BatchSize) + 1
        $runReport = Join-Path $invocationRoot ('{0}-colocation-main-batch-{1:D3}-run.json' -f $invocationLabel, $batchNumber)
        Write-Host "[High-FPS pilot] Running batch $batchNumber; completed=$($progress.progress.stage_completed_runs)/$BatchSize; fps_multiplier=$FpsMultiplier"
        python -m gaugur_lite run `
            --plan $highFpsPlanPath `
            --stage colocation-main `
            --resume `
            --batch-number $batchNumber `
            --batch-size $BatchSize `
            --fail-fast `
            --report $runReport | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "High-FPS batch $batchNumber failed; artifacts preserved." }
        $progressRaw = python -m gaugur_lite run --plan $highFpsPlanPath --stage colocation-main --resume --dry-run
        if ($LASTEXITCODE -ne 0) { throw 'High-FPS runner progress check failed.' }
        $progress = ($progressRaw | Out-String | ConvertFrom-Json)
    }

    Write-Host '[High-FPS pilot] Auditing measured retention for non-degenerate QoS labels...'
    $auditRaw = python -m gaugur_lite effectiveness audit-high-fps-pilot `
        --plan $highFpsPlanPath `
        --solo-baselines $soloPath `
        --qos-ratio $QosRatio `
        --min-completed-runs $BatchSize `
        --min-positive-targets 4 `
        --min-negative-targets 4 `
        --fps-multiplier $FpsMultiplier `
        --output $pilotAcceptance 2>&1
    $auditSucceeded = $LASTEXITCODE -eq 0
    $auditRaw | Out-Host
    if (-not $auditSucceeded) {
        throw 'High-FPS pilot did not produce non-degenerate QoS labels; do not start full collection.'
    }
    Write-Host "PASS high-FPS effectiveness pilot: $pilotPath"
    Write-Host '[High-FPS pilot] Pilot only completed. Full 216-run truth collection is intentionally not started.'
}
finally {
    Remove-Item Env:GAUGUR_WORKLOAD_FPS_MULTIPLIER -ErrorAction SilentlyContinue
    Pop-Location
}
