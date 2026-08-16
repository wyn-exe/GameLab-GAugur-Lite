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
$config = Join-Path $repoRoot 'configs\local.remaining-s30.yaml'
$experiment = Join-Path $repoRoot 'configs\experiments\formal.yaml'
$workloads = Join-Path $repoRoot 'configs\workloads.yaml'
$amendment = Join-Path $repoRoot 'artifacts\profiles\step7\duration-amendment.json'
$plan = Join-Path $repoRoot 'artifacts\plans\formal-v1-remaining-s30.csv'
$planFiles = @(
    $plan,
    $plan.Replace('.csv', '-manifest.json'),
    $plan.Replace('.csv', '-combinations.json')
)
$existingPlanFiles = @($planFiles | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })

Push-Location $repoRoot
try {
    if ($existingPlanFiles.Count -eq 0) {
        # normal 会把未跟踪目录压缩为单行；仍能拒绝 dirty，又不会打印数千个 raw 文件。
        git rev-parse --is-inside-work-tree | Out-Null
        if (-not $?) {
            throw 'git status failed'
        }
        if ((git status --porcelain=v1 --untracked-files=normal | Measure-Object).Count -ne 0) {
            throw 'The worktree must be clean before generating the short-protocol plan; run git status first.'
        }
        Write-Host '[Short protocol] Generating one clean-commit 720-row plan for all stages...'
        $planRaw = python -m gaugur_lite plan `
            --experiment $experiment `
            --stage all `
            --out $plan `
            --config $config `
            --workloads $workloads
        $planSucceeded = $?
        if (-not $planSucceeded) {
            $planRaw | Out-Host
            throw 'Plan generation failed'
        }
    }
    elseif ($existingPlanFiles.Count -ne $planFiles.Count) {
        throw "Partial short-protocol plans exist; stop for audit: $($existingPlanFiles -join ', ')"
    }

    Write-Host '[Short protocol] Verifying the immutable 720-row all-stage plan...'
    $verifyRaw = python -m gaugur_lite plan-verify --plan $plan
    $verificationSucceeded = $?
    if (-not $verificationSucceeded) {
        $verifyRaw | Out-Host
        throw 'Plan verification failed'
    }
    $verified = ($verifyRaw | Out-String) | ConvertFrom-Json
    if ($verified.status -ne 'passed' -or [int]$verified.row_count -ne 720) {
        throw 'Short plan must pass verification with exactly 720 rows'
    }

    Write-Host '[Short protocol] Recomputing the sealed t84 trial and all-stage compatibility...'
    $amendmentRaw = python scripts\build_step7_duration_amendment.py --output $amendment
    $amendmentSucceeded = $?
    if (-not $amendmentSucceeded) {
        $amendmentRaw | Out-Host
        throw 'Short-protocol amendment evidence failed'
    }
    $amendmentResult = ($amendmentRaw | Out-String) | ConvertFrom-Json

    Write-Host '[Short protocol] Auditing reused solo/calibration denominators for s30 profile...'
    $auditRaw = python -m gaugur_lite features build-profiles `
        --plan artifacts\plans\formal-v1-remaining-s30.csv `
        --baseline-plan artifacts\plans\formal-v1.csv `
        --solo-baselines data\interim\formal-v1\solo-baselines.json `
        --calibration artifacts\calibration\step4\formal-calibration.json `
        --out data\interim\formal-v1\profiles.parquet `
        --runs-out data\interim\formal-v1\profile-runs.jsonl `
        --summary data\interim\formal-v1\profile-summary.json `
        --plot-dir artifacts\profiles\step7\s30\plots `
        --dry-run
    $auditSucceeded = $?
    if (-not $auditSucceeded) {
        $auditRaw | Out-Host
        throw 'Short profile input audit failed'
    }
    $audit = ($auditRaw | Out-String) | ConvertFrom-Json
    if ($audit.baseline_contract -ne 'short_profile_amendment_s30_v2' `
            -or [double]$audit.gpu_temperature_max_c -ne 84 `
            -or [double]$audit.profile_amendment.profile_protocol.warmup_s -ne 10 `
            -or [double]$audit.profile_amendment.profile_protocol.duration_s -ne 30 `
            -or [double]$audit.profile_amendment.profile_protocol.cooldown_s -ne 10) {
        throw 'Profile audit did not activate the expected 10/30/10 + t84 contract'
    }

    Write-Host ("PASS short protocol: plan rows=720, remaining rows={0}, nominal hours={1:N2}, excluded t84 valid runs={2}" -f `
        $amendmentResult.remaining_rows,
        $amendmentResult.short_nominal_hours,
        $amendmentResult.t84_valid_runs_excluded)
    Write-Host "Amendment evidence written to: $amendment"
}
finally {
    Pop-Location
}
