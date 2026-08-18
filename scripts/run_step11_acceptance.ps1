param(
    [string]$DatasetDirectory = 'data\processed\formal-v1',
    [string]$SpecFile = 'configs\experiments\ablations.yaml',
    [string]$OutputDirectory = 'artifacts\reports\formal-v1\ablations',
    [int]$BootstrapRepeats = 200
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$datasetPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $DatasetDirectory))
$specPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $SpecFile))
$outputPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDirectory))
$step9Verification = Join-Path $repoRoot 'artifacts\dataset\step9\formal-dataset-verification.json'
$step10Acceptance = Join-Path $repoRoot 'artifacts\models\formal-v1\formal-model-acceptance.json'
$acceptancePath = Join-Path $outputPath 'formal-ablation-acceptance.json'
$artifactRoot = $outputPath
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
        $specPath,
        $step9Verification,
        $step10Acceptance
    )
    $missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
    if ($missing.Count -gt 0) {
        throw "Missing Step 11 inputs: $($missing -join ', ')"
    }
    $step9 = Get-Content -LiteralPath $step9Verification -Raw | ConvertFrom-Json
    if ($step9.status -ne 'passed' -or [int]$step9.passed_count -ne [int]$step9.check_count) {
        throw 'Step 9 verification must pass before Step 11 ablation.'
    }
    $step10 = Get-Content -LiteralPath $step10Acceptance -Raw | ConvertFrom-Json
    if ($step10.status -ne 'passed' -or [int]$step10.evaluation_passed_count -ne [int]$step10.evaluation_check_count) {
        throw 'Step 10 acceptance must pass before Step 11 ablation.'
    }

    $sourceChanges = @(git status --short -- README.md gaugur_lite configs pyproject.toml scripts tests)
    if ($sourceChanges.Count -gt 0) {
        throw "Commit Step 11 source/config changes before ablation:`n$($sourceChanges -join "`n")"
    }

    New-Item -ItemType Directory -Force -Path $invocationRoot | Out-Null
    $invocationNumber = 1
    while (Test-Path -LiteralPath (Join-Path $invocationRoot ('invocation-{0:D3}-unit-tests.txt' -f $invocationNumber))) {
        $invocationNumber += 1
    }
    $unitLog = Join-Path $invocationRoot ('invocation-{0:D3}-unit-tests.txt' -f $invocationNumber)
    Write-Host "[Step 11/invocation-$('{0:D3}' -f $invocationNumber)] Running unit tests before ablation..."
    $unitOutput = python -m pytest tests\unit -q -p no:cacheprovider `
        --basetemp (Join-Path '.test-tmp' ('step11-invocation-{0:D3}-unit' -f $invocationNumber)) 2>&1
    $unitSucceeded = $?
    $unitOutput | Out-File -LiteralPath $unitLog -Encoding utf8
    $unitOutput | Out-Host
    if (-not $unitSucceeded) {
        throw 'Unit tests failed; no ablation was started.'
    }

    $resultFiles = @('ablation-summary.json', 'ablation-rm-mae.png') | ForEach-Object { Join-Path $outputPath $_ }
    $existing = @($resultFiles | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    if ($existing.Count -ne 0 -and $existing.Count -ne $resultFiles.Count) {
        throw "Partial ablation outputs exist; stop for audit: $($existing -join ', ')"
    }
    if ($existing.Count -eq 0) {
        Write-Host '[Step 11] Running feature, label and pair-to-triple ablations...'
        python -m gaugur_lite ablate `
            --dataset-dir $datasetPath `
            --spec $specPath `
            --bootstrap-repeats $BootstrapRepeats `
            --out $outputPath | Out-Host
        if (-not $?) {
            throw 'Step 11 ablation execution failed.'
        }
    }

    if (Test-Path -LiteralPath $acceptancePath -PathType Leaf) {
        throw "Step 11 acceptance artifact already exists; do not overwrite: $acceptancePath"
    }
    $summary = Get-Content -LiteralPath (Join-Path $outputPath 'ablation-summary.json') -Raw | ConvertFrom-Json
    if ($summary.status -ne 'passed' -or [int]$summary.passed_count -ne [int]$summary.check_count) {
        throw 'Step 11 ablation quality gates did not pass.'
    }
    $failedChecks = @($summary.checks.PSObject.Properties | Where-Object { -not [bool]$_.Value })
    if ($failedChecks.Count -gt 0) {
        throw "Step 11 failed checks: $($failedChecks.Name -join ', ')"
    }
    [ordered]@{
        schema_version = 1
        status = 'passed'
        dataset_directory = Get-RelativePath $datasetPath
        spec_file = Get-RelativePath $specPath
        output_directory = Get-RelativePath $outputPath
        variant_count = @($summary.variants).Count
        passed_variant_count = @($summary.variants | Where-Object { $_.status -eq 'passed' }).Count
        skipped_variant_count = @($summary.variants | Where-Object { $_.status -eq 'skipped' }).Count
        check_count = [int]$summary.check_count
        passed_count = [int]$summary.passed_count
        bootstrap_repeats = [int]$summary.bootstrap_repeats
        summary = Get-RelativePath (Join-Path $outputPath 'ablation-summary.json')
        rm_mae_plot = Get-RelativePath (Join-Path $outputPath 'ablation-rm-mae.png')
    } | ConvertTo-Json | Out-File -LiteralPath $acceptancePath -Encoding utf8
    Write-Host "PASS Step 11 formal ablation acceptance: $artifactRoot"
}
finally {
    Pop-Location
}

