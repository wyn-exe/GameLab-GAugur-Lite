param(
    [string]$DatasetDirectory = 'data\processed\formal-v1',
    [string]$ModelDirectory = 'artifacts\models\formal-v1',
    [string]$EvaluationDirectory = 'artifacts\reports\formal-v1\evaluation',
    [int]$BootstrapRepeats = 200
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$datasetPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $DatasetDirectory))
$modelPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $ModelDirectory))
$evaluationPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $EvaluationDirectory))
$step9Verification = Join-Path $repoRoot 'artifacts\dataset\step9\formal-dataset-verification.json'
$modelAcceptance = Join-Path $modelPath 'formal-model-acceptance.json'
$artifactRoot = Join-Path $repoRoot 'artifacts\models\formal-v1'
$invocationRoot = Join-Path $artifactRoot 'invocations'

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
        $step9Verification
    )
    $missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
    if ($missing.Count -gt 0) {
        throw "Missing Step 10 inputs: $($missing -join ', ')"
    }
    $sourceChanges = @(git status --short -- README.md gaugur_lite configs pyproject.toml scripts tests)
    if ($sourceChanges.Count -gt 0) {
        throw "Commit Step 10 source/config changes before model training:`n$($sourceChanges -join "`n")"
    }
    $step9 = Get-Content -LiteralPath $step9Verification -Raw | ConvertFrom-Json
    if ($step9.status -ne 'passed' -or [int]$step9.passed_count -ne [int]$step9.check_count) {
        throw 'Step 9 verification must pass before Step 10 training.'
    }

    New-Item -ItemType Directory -Force -Path $invocationRoot | Out-Null
    $invocationNumber = 1
    while (Test-Path -LiteralPath (Join-Path $invocationRoot ('invocation-{0:D3}-unit-tests.txt' -f $invocationNumber))) {
        $invocationNumber += 1
    }
    $unitLog = Join-Path $invocationRoot ('invocation-{0:D3}-unit-tests.txt' -f $invocationNumber)
    Write-Host "[Step 10/invocation-$('{0:D3}' -f $invocationNumber)] Running unit tests before model training..."
    $unitOutput = python -m pytest tests\unit -q -p no:cacheprovider `
        --basetemp (Join-Path '.test-tmp' ('step10-invocation-{0:D3}-unit' -f $invocationNumber)) 2>&1
    $unitSucceeded = $?
    $unitOutput | Out-File -LiteralPath $unitLog -Encoding utf8
    $unitOutput | Out-Host
    if (-not $unitSucceeded) {
        throw 'Unit tests failed; no model training was started.'
    }

    $modelFiles = @('cm.joblib', 'rm.joblib', 'baselines.joblib', 'model-manifest.json', 'candidate-metrics.json', 'training-summary.json') | ForEach-Object { Join-Path $modelPath $_ }
    $existingModels = @($modelFiles | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    if ($existingModels.Count -ne 0 -and $existingModels.Count -ne $modelFiles.Count) {
        throw "Partial model outputs exist; stop for audit: $($existingModels -join ', ')"
    }
    if ($existingModels.Count -eq 0) {
        Write-Host '[Step 10] Training CM/RM candidates and five baselines...'
        python -m gaugur_lite train `
            --dataset-dir $datasetPath `
            --task both `
            --split-manifest (Join-Path $datasetPath 'split_manifest.json') `
            --seed 20260811 `
            --out $modelPath | Out-Host
        if (-not $?) {
            throw 'Step 10 model training failed.'
        }
    }

    $evaluationFiles = @('evaluation-summary.json', 'rm-error-cdf.png', 'cm-confusion-matrices.png') | ForEach-Object { Join-Path $evaluationPath $_ }
    $existingEvaluation = @($evaluationFiles | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    if ($existingEvaluation.Count -ne 0 -and $existingEvaluation.Count -ne $evaluationFiles.Count) {
        throw "Partial evaluation outputs exist; stop for audit: $($existingEvaluation -join ', ')"
    }
    if ($existingEvaluation.Count -eq 0) {
        Write-Host '[Step 10] Evaluating test and extra_test with combination bootstrap...'
        python -m gaugur_lite evaluate `
            --model-dir $modelPath `
            --dataset-dir $datasetPath `
            --splits test,extra_test `
            --bootstrap-repeats $BootstrapRepeats `
            --out $evaluationPath | Out-Host
        if (-not $?) {
            throw 'Step 10 model evaluation failed.'
        }
    }

    if (Test-Path -LiteralPath $modelAcceptance -PathType Leaf) {
        throw "Step 10 acceptance artifact already exists; do not overwrite: $modelAcceptance"
    }
    $evaluation = Get-Content -LiteralPath (Join-Path $evaluationPath 'evaluation-summary.json') -Raw | ConvertFrom-Json
    if ($evaluation.status -ne 'passed' -or [int]$evaluation.passed_count -ne [int]$evaluation.check_count) {
        throw 'Step 10 evaluation quality gates did not pass.'
    }
    $training = Get-Content -LiteralPath (Join-Path $modelPath 'training-summary.json') -Raw | ConvertFrom-Json
    [ordered]@{
        schema_version = 1
        status = 'passed'
        dataset_directory = Get-RelativePath $datasetPath
        model_directory = Get-RelativePath $modelPath
        evaluation_directory = Get-RelativePath $evaluationPath
        selected_cm_candidate = [string]$training.cm.selected_candidate
        selected_rm_candidate = [string]$training.rm.selected_candidate
        evaluation_check_count = [int]$evaluation.check_count
        evaluation_passed_count = [int]$evaluation.passed_count
        bootstrap_repeats = [int]$evaluation.bootstrap_repeats
        evaluation_summary = Get-RelativePath (Join-Path $evaluationPath 'evaluation-summary.json')
    } | ConvertTo-Json | Out-File -LiteralPath $modelAcceptance -Encoding utf8
    Write-Host "PASS Step 10 formal model acceptance: $artifactRoot"
}
finally {
    Pop-Location
}
