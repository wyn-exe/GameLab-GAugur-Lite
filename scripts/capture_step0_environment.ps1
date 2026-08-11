param(
    [string]$PythonExe = "D:\anaconda3\envs\gaugur-lite\python.exe",
    [string]$OutputDirectory = "artifacts\environment\step0",
    [int]$IdleDurationSeconds = 60
)

$ErrorActionPreference = "Stop"

# Keep every artifact under the repository for reproducible acceptance records.
$scriptFile = $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($scriptFile)) {
    throw "Cannot determine the capture script path"
}
$scriptDirectory = [System.IO.Path]::GetDirectoryName($scriptFile)
$repoRoot = [System.IO.Path]::GetFullPath(
    [System.IO.Path]::Combine($scriptDirectory, "..")
)
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot "README.md") -PathType Leaf)) {
    throw "Calculated repository root is invalid: $repoRoot"
}

$pythonPath = (Resolve-Path -LiteralPath $PythonExe).Path
if ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
    $outputPath = [System.IO.Path]::GetFullPath($OutputDirectory)
} else {
    $outputPath = [System.IO.Path]::GetFullPath(
        [System.IO.Path]::Combine($repoRoot, $OutputDirectory)
    )
}
New-Item -ItemType Directory -Path $outputPath -Force | Out-Null

# Measure idle state before slower WMI, package, and checksum enumeration.
& $pythonPath (Join-Path $repoRoot "scripts\capture_idle_baseline.py") `
    --duration $IdleDurationSeconds `
    --interval 1 `
    --gpu-index 0 `
    --output (Join-Path $outputPath "idle-telemetry.jsonl") `
    --summary (Join-Path $outputPath "idle-summary.json") `
    2>&1 | Out-File (Join-Path $outputPath "idle-command-output.txt") -Encoding utf8
$baselineExitCode = $LASTEXITCODE
if ($baselineExitCode -ne 0) {
    throw "Idle baseline quality gate failed (exit $baselineExitCode). See idle-summary.json"
}

& $pythonPath --version 2>&1 | Out-File (Join-Path $outputPath "python-version.txt") -Encoding utf8
& $pythonPath -m pip list --format=freeze | Out-File (Join-Path $outputPath "pip-freeze.txt") -Encoding utf8
& $pythonPath -m pip check 2>&1 | Out-File (Join-Path $outputPath "pip-check.txt") -Encoding utf8
nvidia-smi --query-gpu=timestamp,name,driver_version,pstate,memory.total,memory.used,utilization.gpu,temperature.gpu,power.draw --format=csv | Out-File (Join-Path $outputPath "nvidia-smi.txt") -Encoding utf8
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader | Out-File (Join-Path $outputPath "gpu-summary.txt") -Encoding utf8
$computerInfoProperties = @(
    "WindowsProductName",
    "WindowsVersion",
    "OsName",
    "OsVersion",
    "OsBuildNumber",
    "OsArchitecture",
    "CsSystemType",
    "CsProcessors",
    "CsNumberOfLogicalProcessors",
    "CsPhyicallyInstalledMemory",
    "CsTotalPhysicalMemory"
)
Get-ComputerInfo -Property $computerInfoProperties |
    Out-File (Join-Path $outputPath "computer-info.txt") -Encoding utf8
powercfg /getactivescheme | Out-File (Join-Path $outputPath "power-plan.txt") -Encoding utf8

# Record common desktop interference without terminating user processes.
Get-Process |
    Where-Object { $_.ProcessName -match "chrome|msedge|firefox|steam|epic|discord|obs" } |
    Select-Object ProcessName, Id, CPU, WorkingSet64 |
    Format-Table -AutoSize |
    Out-File (Join-Path $outputPath "potential-interference-processes.txt") -Encoding utf8

# Verify that the eight upstream games, assets, and licenses are unchanged.
$gameRoot = Join-Path $repoRoot "games\pyxel"
$checksumFile = Join-Path $gameRoot "SHA256SUMS.txt"
$checksumResults = foreach ($line in Get-Content -LiteralPath $checksumFile) {
    if ($line -notmatch "^([A-F0-9]{64})  (.+)$") {
        throw "Cannot parse checksum line: $line"
    }
    $expected = $Matches[1]
    $relative = $Matches[2].Replace("/", "\")
    $target = Join-Path $gameRoot $relative
    $actual = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
    [pscustomobject]@{
        Path = $relative
        Expected = $expected
        Actual = $actual
        Match = ($actual -eq $expected)
    }
}
$checksumResults | ConvertTo-Json -Depth 3 | Out-File (Join-Path $outputPath "game-checksums.json") -Encoding utf8
if ($checksumResults.Match -contains $false) {
    throw "Upstream game SHA-256 verification failed"
}

Write-Output "Step 0 environment artifacts written to: $outputPath"
