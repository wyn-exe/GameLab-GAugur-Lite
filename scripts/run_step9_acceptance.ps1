param(
    [string]$Profiles = 'data\interim\formal-v1\safety-v2\profiles.parquet',
    [string]$Truth = 'data\interim\formal-v1\safety-v2\colocation-truth.parquet',
    [string]$DatasetDirectory = 'data\processed\formal-v1'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$artifactRoot = Join-Path $repoRoot 'artifacts\dataset\step9'
$invocationRoot = Join-Path $artifactRoot 'invocations'
$acceptance = Join-Path $artifactRoot 'formal-dataset-acceptance.json'
$verification = Join-Path $artifactRoot 'formal-dataset-verification.json'
$profilePath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $Profiles))
$truthPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $Truth))
$datasetPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $DatasetDirectory))

function Get-RelativePath([string]$Path) {
    return $Path.Substring($repoRoot.Length + 1).Replace('\', '/')
}

Push-Location $repoRoot
try {
    $required = @($profilePath, $truthPath,
        (Join-Path $repoRoot 'artifacts\colocation\step8\safety-v2\formal-colocation-verification.json'))
    $missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
    if ($missing.Count -gt 0) {
        throw "Missing Step 9 inputs: $($missing -join ', ')"
    }

    $sourceChanges = @(git status --short -- README.md gaugur_lite configs pyproject.toml scripts tests)
    if ($sourceChanges.Count -gt 0) {
        throw "Commit Step 9 source/config changes before dataset build:`n$($sourceChanges -join "`n")"
    }

    $step8Verification = Get-Content -LiteralPath $required[2] -Raw | ConvertFrom-Json
    if ($step8Verification.status -ne 'passed' -or [int]$step8Verification.passed_count -ne [int]$step8Verification.check_count) {
        throw 'Step 8 verification must pass before Step 9 dataset build.'
    }

    New-Item -ItemType Directory -Force -Path $invocationRoot | Out-Null
    $invocationNumber = 1
    while (Test-Path -LiteralPath (Join-Path $invocationRoot ('invocation-{0:D3}-unit-tests.txt' -f $invocationNumber))) {
        $invocationNumber += 1
    }
    $unitLog = Join-Path $invocationRoot ('invocation-{0:D3}-unit-tests.txt' -f $invocationNumber)
    Write-Host "[Step 9/invocation-$('{0:D3}' -f $invocationNumber)] Running unit tests before dataset build..."
    $unitOutput = python -m pytest tests\unit -q -p no:cacheprovider `
        --basetemp (Join-Path '.test-tmp' ('step9-invocation-{0:D3}-unit' -f $invocationNumber)) 2>&1
    $unitSucceeded = $?
    $unitOutput | Out-File -LiteralPath $unitLog -Encoding utf8
    $unitOutput | Out-Host
    if (-not $unitSucceeded) {
        throw 'Unit tests failed; no Step 9 dataset was built.'
    }

    $dryRaw = python -m gaugur_lite features build-dataset `
        --profiles $profilePath `
        --truth $truthPath `
        --out-dir $datasetPath `
        --dry-run
    if (-not $?) {
        $dryRaw | Out-Host
        throw 'Step 9 input preflight failed.'
    }
    $dry = ($dryRaw | Out-String | ConvertFrom-Json)
    if ($dry.status -ne 'passed' -or [int]$dry.profiles_rows -ne 160 -or [int]$dry.truth_rows -ne 600) {
        throw 'Step 9 input row counts do not satisfy the frozen contract.'
    }

    $outputFiles = @(
        'base_samples.parquet', 'rm_samples.parquet', 'cm_samples.parquet',
        'extra_rm_samples.parquet', 'extra_cm_samples.parquet',
        'combination_manifest.json', 'split_manifest.json', 'feature_manifest.json',
        'dataset-summary.json'
    ) | ForEach-Object { Join-Path $datasetPath $_ }
    $existing = @($outputFiles | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    if ($existing.Count -ne 0 -and $existing.Count -ne $outputFiles.Count) {
        throw "Partial Step 9 dataset outputs exist; stop for audit: $($existing -join ', ')"
    }
    if ($existing.Count -eq 0) {
        Write-Host '[Step 9] Building base/RM/CM tables and manifests...'
        python -m gaugur_lite features build-dataset `
            --profiles $profilePath `
            --truth $truthPath `
            --out-dir $datasetPath | Out-Host
        if (-not $?) {
            throw 'Step 9 dataset build failed.'
        }
    }

    Write-Host '[Step 9] Independently recomputing feature rows and artifact hashes...'
    if (Test-Path -LiteralPath $verification -PathType Leaf) {
        throw "Step 9 verification artifact already exists; do not overwrite: $verification"
    }
    $verifyRaw = python -m gaugur_lite features audit `
        --dataset-dir $datasetPath `
        --output $verification
    if (-not $?) {
        $verifyRaw | Out-Host
        throw 'Step 9 independent dataset audit failed.'
    }
    $verified = ($verifyRaw | Out-String | ConvertFrom-Json)
    if ($verified.status -ne 'passed' -or [int]$verified.passed_count -ne [int]$verified.check_count) {
        throw 'Step 9 independent dataset audit returned failed checks.'
    }
    if (Test-Path -LiteralPath $acceptance -PathType Leaf) {
        throw "Step 9 acceptance artifact already exists; do not overwrite: $acceptance"
    }
    [ordered]@{
        schema_version = 1
        status = 'passed'
        profiles = Get-RelativePath $profilePath
        truth = Get-RelativePath $truthPath
        dataset_directory = Get-RelativePath $datasetPath
        base_rows = [int]$verified.row_counts.base
        rm_rows = [int]$verified.row_counts.rm
        cm_rows = [int]$verified.row_counts.cm
        extra_rm_rows = [int]$verified.row_counts.extra_rm
        extra_cm_rows = [int]$verified.row_counts.extra_cm
        verification_check_count = [int]$verified.check_count
        verification_passed_count = [int]$verified.passed_count
        verification = Get-RelativePath $verification
    } | ConvertTo-Json | Out-File -LiteralPath $acceptance -Encoding utf8
    Write-Host "PASS Step 9 formal dataset acceptance: $artifactRoot"
}
finally {
    Pop-Location
}
