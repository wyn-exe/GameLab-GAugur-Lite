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
        -File scripts\run_step11_acceptance.ps1 `
        -BootstrapRepeats $BootstrapRepeats
    if (-not $?) {
        throw 'Step 11 ablation acceptance stopped.'
    }
    Write-Host '[Step 11 final] PASS: ablation variants, bootstrap reports and pair-to-triple protocol verified.'
}
finally {
    Pop-Location
}

