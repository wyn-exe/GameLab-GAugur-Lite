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
$experiment = Join-Path $repoRoot 'configs\experiments\formal.yaml'
$workloads = Join-Path $repoRoot 'configs\workloads.yaml'
$plan = Join-Path $repoRoot 'artifacts\plans\formal-v1-safety-v2-s30.csv'
$pilotEvidence = Join-Path $repoRoot 'artifacts\profiles\step7\safety-v2-amendment.json'
$planVerification = Join-Path $repoRoot 'artifacts\plans\formal-v1-safety-v2-s30-verification.json'
$planContract = Join-Path $repoRoot 'artifacts\plans\formal-v1-safety-v2-s30-contract.json'
$planFiles = @(
    $plan,
    $plan.Replace('.csv', '-manifest.json'),
    $plan.Replace('.csv', '-combinations.json')
)

Push-Location $repoRoot
try {
    $existingPlanFiles = @($planFiles | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    if ($existingPlanFiles.Count -eq 0) {
        if ((git status --porcelain=v1 --untracked-files=normal | Measure-Object).Count -ne 0) {
            throw 'The worktree must be clean before generating the safety-v2 plan.'
        }
        Write-Host '[Safety-v2] Generating one clean-commit 720-row plan...'
        python -m gaugur_lite plan `
            --experiment $experiment `
            --stage all `
            --out $plan `
            --config $config `
            --workloads $workloads | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Safety-v2 plan generation failed with exit code $LASTEXITCODE"
        }
    }
    elseif ($existingPlanFiles.Count -ne $planFiles.Count) {
        throw "Partial safety-v2 plan exists; stop for audit: $($existingPlanFiles -join ', ')"
    }

    Write-Host '[Safety-v2] Verifying plan hashes and pressure mapping...'
    if (Test-Path -LiteralPath $planVerification -PathType Leaf) {
        $verifyRaw = python -m gaugur_lite plan-verify --plan $plan
    }
    else {
        $verifyRaw = python -m gaugur_lite plan-verify --plan $plan --output $planVerification
    }
    if ($LASTEXITCODE -ne 0) {
        $verifyRaw | Out-Host
        throw "Safety-v2 plan verification failed with exit code $LASTEXITCODE"
    }
    $verified = ($verifyRaw | Out-String) | ConvertFrom-Json
    $rows = @(Import-Csv -LiteralPath $plan)
    $profileRows = @($rows | Where-Object { $_.stage -eq 'profile' })
    if ($verified.status -ne 'passed' -or $rows.Count -ne 720 -or $profileRows.Count -ne 480) {
        throw 'Safety-v2 plan must pass verification and contain 720/480 all/profile rows.'
    }
    $protocols = @(
        $profileRows |
            ForEach-Object { "$($_.warmup_s)/$($_.duration_s)/$($_.cooldown_s)/$($_.max_gpu_temp_c)" } |
            Sort-Object -Unique
    )
    if ($protocols.Count -ne 1 -or $protocols[0] -ne '10/30/10/80') {
        throw "Unexpected safety-v2 protocol: $($protocols -join ', ')"
    }

    Write-Host '[Safety-v2] Proving normalized 720-row compatibility with the parent plan...'
    python scripts\build_step7_safety_plan_contract.py `
        --baseline artifacts\plans\formal-v1.csv `
        --safety $plan `
        --output $planContract | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Safety-v2 plan contract failed with exit code $LASTEXITCODE"
    }
    foreach ($row in $profileRows) {
        $expectedApplied = if ($row.resource -eq 'gpu_compute') {
            [double]$row.pressure_requested * 0.25
        }
        else {
            [double]$row.pressure_requested
        }
        if ([Math]::Abs([double]$row.pressure_applied - $expectedApplied) -gt 1e-10) {
            throw "Applied pressure mismatch: $($row.run_id)"
        }
        if ($row.run_directory -notlike 'data/raw/safety-v2-s30/formal-v1/*') {
            throw "Unexpected raw root: $($row.run_id)"
        }
    }

    Write-Host '[Safety-v2] Recomputing sealed s30 pilot evidence...'
    python scripts\build_step7_safety_v2_amendment.py --output $pilotEvidence | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Safety-v2 pilot evidence failed with exit code $LASTEXITCODE"
    }

    $gpuLevels = @(
        $profileRows |
            Where-Object { $_.resource -eq 'gpu_compute' } |
            ForEach-Object { [double]$_.pressure_applied } |
            Sort-Object -Unique
    )
    Write-Host ("PASS safety-v2 plan: rows=720, profile=480, max temp=80 C, GPU applied levels={0}" -f `
        ($gpuLevels -join ','))
    Write-Host "Plan artifacts written to: $([System.IO.Path]::GetDirectoryName($plan))"
}
finally {
    Pop-Location
}
