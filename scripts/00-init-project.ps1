# 00-init-project.ps1 - create the asset tree and persist studio env vars.
# Idempotent: safe to run again at any time.

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $ProjectRoot 'config\studio-env.ps1')
Import-StudioEnv

$Dirs = @(
    'project\00_bible',
    'project\01_dataset\candidates',
    'project\01_dataset\train',
    'project\02_lora\samples',
    'project\03_illust\raw',
    'project\03_illust\final',
    'project\04_manga\panels',
    'project\04_manga\export',
    'project\05_anim\frames',
    'project\05_anim\clips',
    'project\05_anim\final',
    'reports',
    'logs'
)

foreach ($d in $Dirs) {
    $path = Join-Path $ProjectRoot $d
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Path $path | Out-Null
        Write-Host "[ok] created: $d"
    } else {
        Write-Host "[ok] exists : $d"
    }
}

# Keep empty asset folders in git without committing binaries.
foreach ($d in $Dirs) {
    if ($d -like 'project\*') {
        $keep = Join-Path (Join-Path $ProjectRoot $d) '.gitkeep'
        if (-not (Test-Path $keep)) { New-Item -ItemType File -Path $keep | Out-Null }
    }
}

# User-level persistence so a new shell sees the same values.
foreach ($key in $StudioEnv.Keys) {
    [Environment]::SetEnvironmentVariable($key, $StudioEnv[$key], 'User')
    Write-Host "[ok] setx $key=$($StudioEnv[$key])"
}

if (-not (Test-Path $StudioEnv.STUDIO_COMFYUI_DIR)) {
    Write-Host "[warn] ComfyUI not found at $($StudioEnv.STUDIO_COMFYUI_DIR)"
    Write-Host "[info] install the portable build, then re-run or set:"
    Write-Host "[info] setx STUDIO_COMFYUI_DIR <path-to-ComfyUI>"
} else {
    Write-Host "[ok] ComfyUI dir: $($StudioEnv.STUDIO_COMFYUI_DIR)"
}

Write-Host "[ok] project tree ready"
exit 0
