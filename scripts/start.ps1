# start.ps1 - daily entry point: bring ComfyUI up and print the console URL.

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $ProjectRoot 'config\studio-env.ps1')
Import-StudioEnv

& (Join-Path $PSScriptRoot '10-start-comfyui.ps1')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[ok] ComfyUI : http://$($StudioEnv.STUDIO_COMFYUI_HOST)"
Write-Host "[ok] console : http://$($StudioEnv.STUDIO_CONSOLE_HOST):$($StudioEnv.STUDIO_CONSOLE_PORT)"
Write-Host "[info] launch the console with: start.cmd"
exit 0
