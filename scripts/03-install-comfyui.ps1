# 03-install-comfyui.ps1 - install ComfyUI + a cu128 PyTorch next to the project.
#
# Thin wrapper: all logic lives in app/install_comfyui.py so it can be tested and
# reused. Idempotent - re-running resumes a partial download and skips finished
# steps.
#
# Exit codes: 0 installed and verified, 1 failed.

param(
    [string]$TorchVersion = 'auto',
    [switch]$SkipTorch,
    [switch]$ForceSource
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $ProjectRoot 'config\studio-env.ps1')
Import-StudioEnv

$Py = Get-Command python -ErrorAction SilentlyContinue
if (-not $Py) { Write-Error 'python not found on PATH'; exit 1 }

$Args = @((Join-Path $ProjectRoot 'app\install_comfyui.py'),
          '--dir', $StudioEnv.STUDIO_COMFYUI_DIR,
          '--torch-version', $TorchVersion)
if ($SkipTorch) { $Args += '--skip-torch' }
if ($ForceSource) { $Args += '--force-source' }

Write-Host "[..] installing ComfyUI into $($StudioEnv.STUDIO_COMFYUI_DIR)"
& $Py.Source @Args
$rc = $LASTEXITCODE
if ($rc -ne 0) { Write-Host "[warn] installer failed (exit $rc)"; exit $rc }

Write-Host "[ok] ComfyUI installed"
Write-Host "[info] next: .\scripts\10-start-comfyui.ps1"
exit 0
