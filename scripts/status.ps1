# status.ps1 - one screen summary: service, weights, GPU and stage progress.
# Exit code 1 when ComfyUI is down, 0 when it answers.

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $ProjectRoot 'config\studio-env.ps1')
Import-StudioEnv

$Base = "http://$($StudioEnv.STUDIO_COMFYUI_HOST)"

try {
    $stats = Invoke-RestMethod -Uri "$Base/system_stats" -TimeoutSec 4
    Write-Host "[ok] ComfyUI online at $Base"
} catch {
    Write-Host "[down] ComfyUI offline at $Base"
    exit 1
}

Write-Host "== queue =="
try {
    $q = Invoke-RestMethod -Uri "$Base/queue" -TimeoutSec 4
    Write-Host "[info] running=$($q.queue_running.Count) pending=$($q.queue_pending.Count)"
} catch { Write-Host "[warn] queue unavailable" }

Write-Host "== weights =="
$manifest = Get-Content (Join-Path $ProjectRoot 'config\models.manifest.json') -Raw | ConvertFrom-Json
foreach ($f in $manifest.files) {
    $target = Join-Path $StudioEnv.STUDIO_COMFYUI_DIR "models\$($f.target)\$($f.name)"
    if (Test-Path $target) {
        Write-Host "[ok] $($f.name) $([math]::Round((Get-Item $target).Length / 1GB, 2)) GB"
    } else {
        Write-Host "[warn] missing $($f.name)"
    }
}

Write-Host "== gpu =="
$gpu = & nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader 2>$null
if ($gpu) { Write-Host "[ok] $($gpu.Trim()) MB" }

Write-Host "== assets =="
foreach ($d in @('00_bible', '01_dataset', '02_lora', '03_illust', '04_manga', '05_anim')) {
    $path = Join-Path $ProjectRoot "project\$d"
    $n = (Get-ChildItem -Path $path -Recurse -File -ErrorAction SilentlyContinue |
          Where-Object { $_.Name -ne '.gitkeep' }).Count
    Write-Host "[info] ${d}: $n file(s)"
}
exit 0
