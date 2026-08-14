[CmdletBinding()]
param(
    [switch]$PreflightOnly,
    [switch]$Resume
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'README.md') -PathType Leaf)) {
    throw "Repository marker README.md not found under $repoRoot"
}

$artifactRoot = Join-Path $repoRoot 'artifacts\workloads\step3'
$formalRoot = Join-Path $artifactRoot 'formal'
$upstreamOutput = Join-Path $artifactRoot 'upstream-verification.json'
$acceptanceOutput = Join-Path $artifactRoot 'acceptance.json'

$gameFrames = [ordered]@{
    pyxel_jump       = 900
    pyxel_bubbles    = 900
    pyxel_snake      = 600
    pyxel_shooter    = 900
    pyxel_platformer = 900
    daylight         = 300
    mega_wing        = 900
    space_rescue     = 900
}

function Test-CompletedRun {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RunDirectory,
        [Parameter(Mandatory = $true)]
        [int]$ExpectedFrames
    )

    foreach ($requiredName in @('game_metrics.jsonl', 'launcher.json', 'status.json', 'summary.json')) {
        if (-not (Test-Path -LiteralPath (Join-Path $RunDirectory $requiredName) -PathType Leaf)) {
            return $false
        }
    }
    try {
        $launcher = Get-Content -LiteralPath (Join-Path $RunDirectory 'launcher.json') -Raw | ConvertFrom-Json
        $status = Get-Content -LiteralPath (Join-Path $RunDirectory 'status.json') -Raw | ConvertFrom-Json
        $summary = Get-Content -LiteralPath (Join-Path $RunDirectory 'summary.json') -Raw | ConvertFrom-Json
        return (
            $launcher.status -eq 'completed' -and
            $launcher.upstream_unchanged -eq $true -and
            $status.status -eq 'completed' -and
            $summary.status -eq 'completed' -and
            $summary.headless -eq $false -and
            [int]$summary.draw_count -eq $ExpectedFrames -and
            [int]$summary.max_frames -eq $ExpectedFrames
        )
    }
    catch {
        return $false
    }
}

# Check all 24 targets before opening the first game window.
$plannedDirectories = New-Object 'System.Collections.Generic.List[string]'
$completedDirectories = New-Object 'System.Collections.Generic.List[string]'
$invalidDirectories = New-Object 'System.Collections.Generic.List[string]'
foreach ($gameId in $gameFrames.Keys) {
    foreach ($runRepeat in 1..3) {
        $gameRoot = Join-Path -Path $formalRoot -ChildPath $gameId
        $runDirectory = Join-Path -Path $gameRoot -ChildPath ("r{0:D2}" -f $runRepeat)
        [void]$plannedDirectories.Add($runDirectory)
        if (Test-Path -LiteralPath $runDirectory) {
            if (Test-CompletedRun -RunDirectory $runDirectory -ExpectedFrames ([int]$gameFrames[$gameId])) {
                [void]$completedDirectories.Add($runDirectory)
            }
            else {
                [void]$invalidDirectories.Add($runDirectory)
            }
        }
    }
}
$existingCount = $completedDirectories.Count + $invalidDirectories.Count
if (-not $Resume -and $existingCount -gt 0) {
    throw "Formal output already exists; use -Resume only after reviewing preserved runs"
}
if ($Resume -and $invalidDirectories.Count -gt 0) {
    throw "Resume refused because incomplete or failed run directories remain: $($invalidDirectories -join ', ')"
}
if (Test-Path -LiteralPath $upstreamOutput) {
    if (-not $Resume) {
        throw "Upstream verification output already exists: $upstreamOutput"
    }
    $recordedUpstream = Get-Content -LiteralPath $upstreamOutput -Raw | ConvertFrom-Json
    if ($recordedUpstream.status -ne 'passed') {
        throw "Recorded upstream verification did not pass: $upstreamOutput"
    }
}
if (Test-Path -LiteralPath $acceptanceOutput) {
    throw "Acceptance output already exists: $acceptanceOutput"
}

# Validate Windows PowerShell 5.1 parsing and the 8-by-3 plan without writing artifacts.
if ($PreflightOnly) {
    $preflightResult = [ordered]@{
        status               = 'passed'
        powershell_version   = $PSVersionTable.PSVersion.ToString()
        game_count           = $gameFrames.Count
        planned_run_count    = $plannedDirectories.Count
        resume               = [bool]$Resume
        completed_run_count  = $completedDirectories.Count
        remaining_run_count  = $plannedDirectories.Count - $completedDirectories.Count
        invalid_run_count    = $invalidDirectories.Count
    }
    $preflightResult | ConvertTo-Json
    return
}

Push-Location $repoRoot
try {
    Write-Host '[Step 3] Running unit tests...'
    python -m pytest tests\unit -q -p no:cacheprovider
    if ($LASTEXITCODE -ne 0) {
        throw "Unit tests failed with exit code $LASTEXITCODE"
    }

    Write-Host '[Step 3] Verifying immutable upstream files and extracted trees...'
    if ($Resume -and (Test-Path -LiteralPath $upstreamOutput)) {
        python -m gaugur_lite workload verify-upstream --root games\pyxel
    }
    else {
        python -m gaugur_lite workload verify-upstream `
            --root games\pyxel `
            --output $upstreamOutput
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Upstream verification failed with exit code $LASTEXITCODE"
    }

    $remainingCount = $plannedDirectories.Count - $completedDirectories.Count
    Write-Host ("[Step 3] Starting {0} remaining visible-window runs..." -f $remainingCount)
    foreach ($runRepeat in 1..3) {
        foreach ($gameId in $gameFrames.Keys) {
            $frames = [int]$gameFrames[$gameId]
            $gameRoot = Join-Path -Path $formalRoot -ChildPath $gameId
            $runDirectory = Join-Path -Path $gameRoot -ChildPath ("r{0:D2}" -f $runRepeat)
            if ($completedDirectories.Contains($runDirectory)) {
                Write-Host ("SKIP {0} r{1:D2}: preserved completed run" -f $gameId, $runRepeat)
                continue
            }
            Write-Host ("Running {0} repeat {1}/3, {2} frames..." -f $gameId, $runRepeat, $frames)
            $rawResult = python -m gaugur_lite workload smoke $gameId `
                --duration 30 `
                --max-frames $frames `
                --repeat $runRepeat `
                --output-directory $runDirectory
            if ($LASTEXITCODE -ne 0) {
                $rawResult | Out-Host
                throw "Workload $gameId repeat $runRepeat failed with exit code $LASTEXITCODE"
            }
            $result = ($rawResult | Out-String) | ConvertFrom-Json
            Write-Host ("PASS {0} r{1:D2}: mean FPS={2:N3}, p05 FPS={3:N3}, missed={4}" -f `
                $gameId,
                $runRepeat,
                [double]$result.summary.game_fps.mean,
                [double]$result.summary.game_fps.p05,
                [int]$result.summary.missed_deadline_count)
        }
    }

    Write-Host '[Step 3] Aggregating repeat consistency, window health and FPS CV...'
    python -m gaugur_lite workload accept `
        --input-root $formalRoot `
        --expected-repeats 3 `
        --output $acceptanceOutput
    if ($LASTEXITCODE -ne 0) {
        throw "Step 3 acceptance failed with exit code $LASTEXITCODE; results were preserved"
    }

    Write-Host "Step 3 acceptance artifacts written to: $artifactRoot"
}
finally {
    Pop-Location
}
