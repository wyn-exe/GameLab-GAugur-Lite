[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pwsh = (Get-Command pwsh.exe -ErrorAction Stop).Source
Push-Location $repoRoot
try {
    & $pwsh -NoProfile -ExecutionPolicy Bypass `
        -File scripts\run_step12_acceptance.ps1
    if (-not $?) {
        throw 'Step 12 QoS-safe packing acceptance stopped.'
    }
    Write-Host '[Step 12 final] PASS: QoS-safe packing, measured truth audit and baseline comparison verified.'
}
finally {
    Pop-Location
}
