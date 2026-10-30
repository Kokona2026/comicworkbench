# stop.ps1 - stop ComfyUI and release VRAM.
# NOTE: uses `return` so it stays safe to call from an orchestration script.

$ErrorActionPreference = 'SilentlyContinue'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $ProjectRoot 'config\studio-env.ps1')

& (Join-Path $PSScriptRoot '11-stop-comfyui.ps1')

$gpu = & nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>$null
if ($gpu) { Write-Host "[gpu] VRAM used: $($gpu.Trim()) MB" }
return
