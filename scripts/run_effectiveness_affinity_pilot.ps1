[CmdletBinding()]
param(
    [int]$BatchSize = 12,
    [double]$FpsMultiplier = 8.0,
    [string]$CpuAffinity = '0',
    [double]$QosRatio = 0.80
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# 复用高帧率 pilot 的全套质量门，但使用独立计划、基线和 raw 根。
& (Join-Path $PSScriptRoot 'run_effectiveness_highfps_pilot.ps1') `
    -BasePlan 'artifacts\plans\formal-v1-safety-v2-s30.csv' `
    -LocalConfig 'configs\local.safety-v2-s30.yaml' `
    -HighFpsPlan 'artifacts\plans\formal-highfps-affinity-v1.csv' `
    -SoloBaselines 'data\interim\formal-highfps-affinity-v1\solo-baselines.json' `
    -SoloRuns 'data\interim\formal-highfps-affinity-v1\solo-runs.jsonl' `
    -SoloPlot 'artifacts\effectiveness\affinity-pilot\solo-baselines.png' `
    -PilotDirectory 'artifacts\effectiveness\affinity-pilot' `
    -BatchSize $BatchSize `
    -FpsMultiplier $FpsMultiplier `
    -QosRatio $QosRatio `
    -ExperimentId 'formal-highfps-affinity-v1' `
    -CpuAffinity $CpuAffinity
exit $LASTEXITCODE
