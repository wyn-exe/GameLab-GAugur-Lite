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
$plan = Join-Path $repoRoot 'artifacts\plans\formal-v1-profile-t84.csv'
$manifest = Join-Path $repoRoot 'artifacts\plans\formal-v1-profile-t84-manifest.json'
$combinations = Join-Path $repoRoot 'artifacts\plans\formal-v1-profile-t84-combinations.json'
$deviceQuery = Join-Path $repoRoot 'artifacts\profiles\step7\thermal-device-query.txt'
$amendment = Join-Path $repoRoot 'artifacts\profiles\step7\thermal-amendment.json'
$planFiles = @($plan, $manifest, $combinations)
$existingPlanFiles = @($planFiles | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })

Push-Location $repoRoot
try {
    if ($existingPlanFiles.Count -eq 0) {
        $dirty = @(git status --porcelain=v1 --untracked-files=all)
        if ($LASTEXITCODE -ne 0) {
            throw 'git status failed'
        }
        if ($dirty.Count -ne 0) {
            throw "生成温控修订计划前工作树必须干净；请先提交并上传 Step 7 pilot 与修订代码。`n$($dirty -join "`n")"
        }
        Write-Host '[Step 7 amendment] Generating an immutable 480-row profile-only plan from the clean commit...'
        $planRaw = python -m gaugur_lite plan `
            --experiment configs\experiments\formal.yaml `
            --stage profile `
            --out $plan `
            --config configs\local.step7-t84.yaml `
            --workloads configs\workloads.yaml
        if ($LASTEXITCODE -ne 0) {
            $planRaw | Out-Host
            throw "Amended plan generation failed with exit code $LASTEXITCODE"
        }
    }
    elseif ($existingPlanFiles.Count -ne $planFiles.Count) {
        throw "Partial amended plan exists; stop for audit: $($existingPlanFiles -join ', ')"
    }

    Write-Host '[Step 7 amendment] Verifying the amended plan...'
    $verifyRaw = python -m gaugur_lite plan-verify --plan $plan
    if ($LASTEXITCODE -ne 0) {
        $verifyRaw | Out-Host
        throw "Amended plan verification failed with exit code $LASTEXITCODE"
    }
    $verified = ($verifyRaw | Out-String) | ConvertFrom-Json
    if ($verified.status -ne 'passed' -or [int]$verified.row_count -ne 480) {
        throw 'Amended plan must pass verification and contain exactly 480 rows'
    }

    if (-not (Test-Path -LiteralPath $deviceQuery -PathType Leaf)) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $deviceQuery) | Out-Null
        nvidia-smi -q -d TEMPERATURE | Out-File -LiteralPath $deviceQuery -Encoding utf8
        if ($LASTEXITCODE -ne 0) {
            throw "nvidia-smi temperature query failed with exit code $LASTEXITCODE"
        }
    }

    Write-Host '[Step 7 amendment] Recomputing pilot evidence and strict parent-plan compatibility...'
    $amendmentRaw = python scripts\build_step7_thermal_amendment.py --output $amendment
    if ($LASTEXITCODE -ne 0) {
        $amendmentRaw | Out-Host
        throw "Thermal amendment evidence failed with exit code $LASTEXITCODE"
    }
    $amendmentResult = ($amendmentRaw | Out-String) | ConvertFrom-Json

    Write-Host '[Step 7 amendment] Auditing reused solo/calibration denominators...'
    $auditRaw = python -m gaugur_lite features build-profiles `
        --plan $plan `
        --baseline-plan artifacts\plans\formal-v1.csv `
        --solo-baselines data\interim\formal-v1\solo-baselines.json `
        --calibration artifacts\calibration\step4\formal-calibration.json `
        --out data\interim\formal-v1\profiles.parquet `
        --runs-out data\interim\formal-v1\profile-runs.jsonl `
        --summary data\interim\formal-v1\profile-summary.json `
        --plot-dir artifacts\profiles\step7\t84\plots `
        --dry-run
    if ($LASTEXITCODE -ne 0) {
        $auditRaw | Out-Host
        throw "Amendment input audit failed with exit code $LASTEXITCODE"
    }
    $audit = ($auditRaw | Out-String) | ConvertFrom-Json
    if ($audit.baseline_contract -ne 'thermal_profile_amendment_v1' `
            -or [double]$audit.gpu_temperature_max_c -ne 84) {
        throw 'Profile audit did not activate the expected t84 amendment contract'
    }

    Write-Host ("PASS Step 7 thermal amendment: rows=480, plan SHA-256={0}, pilot=23, repeated trigger attempts=4" -f `
        $verified.plan_sha256)
    Write-Host "Amendment evidence written to: $amendment"
}
finally {
    Pop-Location
}
