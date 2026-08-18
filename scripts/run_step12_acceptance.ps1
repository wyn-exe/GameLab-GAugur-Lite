param(
    [string]$DatasetDirectory = 'data\processed\formal-v1',
    [string]$ModelDirectory = 'artifacts\models\formal-v1',
    [string]$RequestsFile = 'configs\requests\formal.yaml',
    [string]$GroundTruthFile = 'data\interim\formal-v1\safety-v2\colocation-truth.parquet',
    [string]$OutputDirectory = 'artifacts\reports\formal-v1\packing',
    [double]$QosRatio = 0.80
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$datasetPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $DatasetDirectory))
$modelPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $ModelDirectory))
$requestsPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $RequestsFile))
$truthPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $GroundTruthFile))
$outputPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDirectory))
$step9Verification = Join-Path $repoRoot 'artifacts\dataset\step9\formal-dataset-verification.json'
$step10Acceptance = Join-Path $repoRoot 'artifacts\models\formal-v1\formal-model-acceptance.json'
$step11Acceptance = Join-Path $repoRoot 'artifacts\reports\formal-v1\ablations\formal-ablation-acceptance.json'
$acceptancePath = Join-Path $outputPath 'formal-packing-acceptance.json'
$invocationRoot = Join-Path $outputPath 'invocations'

function Get-RelativePath([string]$Path) {
    return $Path.Substring($repoRoot.Length + 1).Replace('\', '/')
}

Push-Location $repoRoot
try {
    $required = @(
        (Join-Path $datasetPath 'feature_manifest.json'),
        (Join-Path $datasetPath 'split_manifest.json'),
        (Join-Path $datasetPath 'rm_samples.parquet'),
        (Join-Path $datasetPath 'cm_samples.parquet'),
        (Join-Path $modelPath 'cm.joblib'),
        (Join-Path $modelPath 'baselines.joblib'),
        (Join-Path $modelPath 'model-manifest.json'),
        $requestsPath,
        $truthPath,
        $step9Verification,
        $step10Acceptance,
        $step11Acceptance
    )
    $missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
    if ($missing.Count -gt 0) {
        throw "Missing Step 12 inputs: $($missing -join ', ')"
    }
    if (Test-Path -LiteralPath $acceptancePath -PathType Leaf) {
        throw "Step 12 acceptance artifact already exists; do not overwrite: $acceptancePath"
    }

    $step9 = Get-Content -LiteralPath $step9Verification -Raw | ConvertFrom-Json
    if ($step9.status -ne 'passed' -or [int]$step9.passed_count -ne [int]$step9.check_count) {
        throw 'Step 9 verification must pass before Step 12 replay.'
    }
    $step10 = Get-Content -LiteralPath $step10Acceptance -Raw | ConvertFrom-Json
    if ($step10.status -ne 'passed' -or [int]$step10.evaluation_passed_count -ne [int]$step10.evaluation_check_count) {
        throw 'Step 10 acceptance must pass before Step 12 replay.'
    }
    $step11 = Get-Content -LiteralPath $step11Acceptance -Raw | ConvertFrom-Json
    if ($step11.status -ne 'passed' -or [int]$step11.passed_count -ne [int]$step11.check_count) {
        throw 'Step 11 acceptance must pass before Step 12 replay.'
    }

    $sourceChanges = @(git status --short -- README.md gaugur_lite configs pyproject.toml scripts tests)
    if ($sourceChanges.Count -gt 0) {
        throw "Commit Step 12 source/config changes before formal replay:`n$($sourceChanges -join "`n")"
    }

    New-Item -ItemType Directory -Force -Path $invocationRoot | Out-Null
    $invocationNumber = 1
    while (Test-Path -LiteralPath (Join-Path $invocationRoot ('invocation-{0:D3}-unit-tests.txt' -f $invocationNumber))) {
        $invocationNumber += 1
    }
    $unitLog = Join-Path $invocationRoot ('invocation-{0:D3}-unit-tests.txt' -f $invocationNumber)
    Write-Host "[Step 12/invocation-$('{0:D3}' -f $invocationNumber)] Running unit tests before packing replay..."
    $unitOutput = python -m pytest tests\unit -q -p no:cacheprovider `
        --basetemp (Join-Path '.test-tmp' ('step12-invocation-{0:D3}-unit' -f $invocationNumber)) 2>&1
    $unitSucceeded = $?
    $unitOutput | Out-File -LiteralPath $unitLog -Encoding utf8
    $unitOutput | Out-Host
    if (-not $unitSucceeded) {
        throw 'Unit tests failed; no Step 12 replay was started.'
    }

    $resultFiles = @('packing-summary.json', 'packing-slots.png') | ForEach-Object { Join-Path $outputPath $_ }
    $existing = @($resultFiles | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    if ($existing.Count -ne 0 -and $existing.Count -ne $resultFiles.Count) {
        throw "Partial Step 12 replay outputs exist; stop for audit: $($existing -join ', ')"
    }
    if ($existing.Count -eq 0) {
        Write-Host '[Step 12] Running QoS-safe greedy packing replay and measured-truth audit...'
        python -m gaugur_lite replay pack `
            --model (Join-Path $modelPath 'cm.joblib') `
            --requests $requestsPath `
            --ground-truth $truthPath `
            --dataset-dir $datasetPath `
            --qos-ratio $QosRatio `
            --out $outputPath | Out-Host
        if (-not $?) {
            throw 'Step 12 packing replay failed.'
        }
    }

    $summary = Get-Content -LiteralPath (Join-Path $outputPath 'packing-summary.json') -Raw | ConvertFrom-Json
    if ($summary.status -ne 'passed' -or [int]$summary.passed_count -ne [int]$summary.check_count) {
        throw 'Step 12 packing quality gates did not pass.'
    }
    $failedChecks = @($summary.checks.PSObject.Properties | Where-Object { -not [bool]$_.Value })
    if ($failedChecks.Count -gt 0) {
        throw "Step 12 failed checks: $($failedChecks.Name -join ', ')"
    }
    $strategies = @($summary.strategies)
    $cmReport = $strategies | Where-Object { $_.strategy -eq 'cm_model' }
    $noColocation = $strategies | Where-Object { $_.strategy -eq 'no_colocation' }
    if ($null -eq $cmReport -or $null -eq $noColocation) {
        throw 'Step 12 summary must include cm_model and no_colocation reports.'
    }
    [ordered]@{
        schema_version = 1
        status = 'passed'
        dataset_directory = Get-RelativePath $datasetPath
        model_directory = Get-RelativePath $modelPath
        requests_file = Get-RelativePath $requestsPath
        ground_truth = Get-RelativePath $truthPath
        output_directory = Get-RelativePath $outputPath
        qos_ratio = [double]$summary.qos_ratio
        strategy_count = $strategies.Count
        cm_model_slot_count = [int]$cmReport.slot_count
        no_colocation_slot_count = [int]$noColocation.slot_count
        cm_model_actual_qos_violation_rate = [double]$cmReport.actual_qos_violation_rate
        summary_check_count = [int]$summary.check_count
        summary_passed_count = [int]$summary.passed_count
        summary = Get-RelativePath (Join-Path $outputPath 'packing-summary.json')
        plot = Get-RelativePath (Join-Path $outputPath 'packing-slots.png')
    } | ConvertTo-Json | Out-File -LiteralPath $acceptancePath -Encoding utf8
    Write-Host "PASS Step 12 formal packing acceptance: $outputPath"
}
finally {
    Pop-Location
}
