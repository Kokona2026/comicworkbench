# 01-check-env.ps1 - read-only environment report for stage 0.
# Exit code 0 = ready, 1 = ComfyUI or Python missing.

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $ProjectRoot 'config\studio-env.ps1')
Import-StudioEnv

$fail = 0

Write-Host "== python =="
$py = Get-ComfyUiPython -Root $StudioEnv.STUDIO_COMFYUI_DIR
if (-not $py) {
    # Fall back to any python on PATH when ComfyUI is not installed yet.
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source }
}
if ($py) {
    $ver = & $py -c "import platform,sys;sys.stdout.write(platform.python_version())" 2>$null
    Write-Host "[ok] python: $ver ($py)"
} else {
    Write-Host "[warn] python not found on PATH"
    $fail = 1
}

Write-Host "== comfyui =="
$launcher = Get-ComfyUiLauncher -Root $StudioEnv.STUDIO_COMFYUI_DIR
if ($launcher) {
    Write-Host "[ok] launcher: $launcher"
} else {
    Write-Host "[warn] no main.py under $($StudioEnv.STUDIO_COMFYUI_DIR)"
    Write-Host "[info] install ComfyUI or set: setx STUDIO_COMFYUI_DIR <path>"
    $fail = 1
}

Write-Host "== gpu =="
$gpu = & nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader 2>$null
if ($gpu) { Write-Host "[ok] $($gpu.Trim())" } else { Write-Host "[warn] nvidia-smi unavailable" }

Write-Host "== disk =="
$drv = (Get-Item $ProjectRoot).PSDrive.Name
$d = Get-PSDrive -Name $drv
Write-Host "[ok] drive ${drv}: free $([math]::Round($d.Free / 1GB, 1)) GB"

Write-Host "== ports =="
foreach ($p in @(8188, 8766)) {
    $c = Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue
    if ($c) { Write-Host "[ok] $p listening" } else { Write-Host "[info] $p free" }
}

Write-Host "== weights =="
$manifest = Get-Content (Join-Path $ProjectRoot 'config\models.manifest.json') -Raw | ConvertFrom-Json
foreach ($f in $manifest.files) {
    $target = Join-Path $StudioEnv.STUDIO_COMFYUI_DIR "models\$($f.target)\$($f.name)"
    if (Test-Path $target) {
        $gb = [math]::Round((Get-Item $target).Length / 1GB, 2)
        Write-Host "[ok] $($f.name) ${gb} GB"
    } else {
        Write-Host "[warn] missing: $($f.name) -> models\$($f.target)"
        Write-Host "[info] source: $($f.source_hint)"
    }
}

if ($fail -eq 1) { Write-Host "[warn] environment not ready" } else { Write-Host "[ok] environment ready" }
exit $fail
