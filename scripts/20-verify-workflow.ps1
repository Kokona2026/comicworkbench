# 20-verify-workflow.ps1 - validate workflow templates against the live ComfyUI.
#
# Offline checks always run (JSON shape, placeholder coverage, link resolution).
# When ComfyUI is up, node class names and input names are additionally checked
# against /object_info, which is the only authoritative source for node names.

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $ProjectRoot 'config\studio-env.ps1')
Import-StudioEnv

$Py = Get-Command python -ErrorAction SilentlyContinue
if (-not $Py) { Write-Error 'python not found on PATH'; exit 1 }

& python (Join-Path $ProjectRoot 'tests\workflow_test.py')
$rc = $LASTEXITCODE
if ($rc -ne 0) { Write-Host "[warn] workflow validation failed"; exit $rc }
Write-Host "[ok] workflow validation passed"
exit 0
