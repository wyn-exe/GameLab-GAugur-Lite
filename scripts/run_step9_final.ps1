[CmdletBinding()]
param(
    [string]$Profiles = 'data\interim\formal-v1\safety-v2\profiles.parquet',
    [string]$Truth = 'data\interim\formal-v1\safety-v2\colocation-truth.parquet',
    [string]$DatasetDirectory = 'data\processed\formal-v1'
)

$ErrorActionPreference = 'Stop'
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pwsh = (Get-Command pwsh.exe -ErrorAction Stop).Source
Push-Location $repoRoot
try {
    & $pwsh -NoProfile -ExecutionPolicy Bypass `
        -File scripts\run_step9_acceptance.ps1 `
        -Profiles $Profiles `
        -Truth $Truth `
        -DatasetDirectory $DatasetDirectory
    if (-not $?) {
        throw 'Step 9 dataset acceptance stopped.'
    }
    Write-Host '[Step 9 final] PASS: all model dataset tables and manifests verified.'
}
finally {
    Pop-Location
}
