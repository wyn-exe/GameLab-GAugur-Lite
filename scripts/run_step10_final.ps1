[CmdletBinding()]
param(
    [int]$BootstrapRepeats = 200
)

$ErrorActionPreference = 'Stop'
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pwsh = (Get-Command pwsh.exe -ErrorAction Stop).Source
Push-Location $repoRoot
try {
    & $pwsh -NoProfile -ExecutionPolicy Bypass `
        -File scripts\run_step10_acceptance.ps1 `
        -BootstrapRepeats $BootstrapRepeats
    if (-not $?) {
        throw 'Step 10 model acceptance stopped.'
    }
    Write-Host '[Step 10 final] PASS: CM/RM models, baselines and held-out evaluations verified.'
}
finally {
    Pop-Location
}
