<#
.SYNOPSIS
    Stop duplicate ComfyUI instances, keeping the one that owns the service port.

.DESCRIPTION
    Two ComfyUI trees mean two independent VRAM allocations and two queues, which
    the audit reports as SVC-04. This keeps the process that listens on
    STUDIO_COMFYUI_HOST together with its ancestor launcher (the venv shim), and
    stops any other python process running main.py.

    Idempotent: with a single instance it prints "kept" and exits 0.

.PARAMETER WhatIf
    Report what would be stopped without stopping anything.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\12-dedupe-comfyui.ps1
#>
[CmdletBinding()]
param([switch]$WhatIf)

$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $root 'config\studio-env.ps1')

$port = [int](($StudioEnv.STUDIO_COMFYUI_HOST -split ':')[-1])

$listen = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
    Select-Object -First 1
$listenerPid = 0
if ($listen) { $listenerPid = [int]$listen.OwningProcess }

$parents = @{}
Get-CimInstance Win32_Process | ForEach-Object { $parents[[int]$_.ProcessId] = [int]$_.ParentProcessId }

$matched = @(Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
    Where-Object { $_.CommandLine -match 'main\.py' } |
    Select-Object -ExpandProperty ProcessId)

if ($matched.Count -eq 0) {
    Write-Output '[ok] no ComfyUI process found'
    return
}
if ($listenerPid -eq 0) {
    Write-Output '[ok] ComfyUI is not listening; nothing to dedupe'
    return
}

# Keep the listener and every ancestor: the venv python.exe is a launcher shim.
$keep = @{}
$cursor = $listenerPid
while ($cursor -and -not $keep.ContainsKey($cursor)) {
    $keep[$cursor] = $true
    $cursor = $parents[$cursor]
}

$stopped = 0
foreach ($procId in $matched) {
    $procId = [int]$procId
    if ($procId -eq $listenerPid) { continue }
    if ($keep.ContainsKey($procId)) { continue }
    if ($WhatIf) { Write-Output ("[whatif] stop pid {0}" -f $procId); continue }
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    if (-not (Get-Process -Id $procId -ErrorAction SilentlyContinue)) { $stopped++ }
}

Write-Output ("[ok] kept pid {0}, stopped {1} duplicate process(es)" -f $listenerPid, $stopped)
