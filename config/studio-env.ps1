# studio-env.ps1 - single source of truth for the local anime manga studio.
#
# Dot-sourced by scripts/*.ps1 and by app/checks.ps1. It does not mutate user
# state by itself: callers decide between process-level (Import-StudioEnv) and
# user-level persistence (01-init-project.ps1).
#
# ASCII-only on purpose: Windows PowerShell 5.1 parses a BOM-less .ps1 as ANSI.

# Callers (scripts/*.ps1) define $ProjectRoot before dot-sourcing this file.
# Fall back to the parent of config/ when sourced standalone.
if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}

# NOTE: keep every entry a literal expression. app/studio.py parses this block
# to give Python the same single source of truth, and it can only resolve
# quoted strings and Join-Path $ProjectRoot 'x'.
$StudioEnv = @{
    STUDIO_PROJECT_DIR   = Join-Path $ProjectRoot 'project'
    STUDIO_COMFYUI_DIR   = Join-Path $ProjectRoot 'ComfyUI'
    STUDIO_COMFYUI_HOST  = '127.0.0.1:8188'
    STUDIO_CONSOLE_HOST  = '127.0.0.1'
    STUDIO_CONSOLE_PORT  = '8766'
    STUDIO_MIN_COMFYUI   = '0.3.31'
    STUDIO_OLLAMA_HOST   = '127.0.0.1:11434'
}

# An existing user-level override wins, so an out-of-tree ComfyUI install can be
# reused without editing this file. Applied after the table so the table stays
# literal-only.
$ComfyOverride = [Environment]::GetEnvironmentVariable('STUDIO_COMFYUI_DIR', 'User')
if ($ComfyOverride -and (Test-Path $ComfyOverride)) {
    $StudioEnv.STUDIO_COMFYUI_DIR = $ComfyOverride
}

# Process-level injection: takes effect immediately, no shell restart needed.
function Import-StudioEnv {
    foreach ($key in $StudioEnv.Keys) {
        Set-Item -Path "Env:\$key" -Value $StudioEnv[$key]
    }
}

# Resolves the ComfyUI launcher. Returns $null when ComfyUI is absent so callers
# can emit an actionable hint instead of crashing.
function Get-ComfyUiLauncher {
    param([string]$Root = $StudioEnv.STUDIO_COMFYUI_DIR)

    if (-not $Root -or -not (Test-Path $Root)) { return $null }

    $candidates = @(
        (Join-Path $Root 'main.py'),
        (Join-Path $Root 'run_nvidia_gpu.bat'),
        (Join-Path $Root 'python_embeded\python.exe')
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

# Resolves the interpreter that owns ComfyUI's dependencies.
# Order: portable embedded python, then a venv inside the ComfyUI directory
# (what scripts/03-install-comfyui.ps1 creates), then whatever is on PATH.
function Get-ComfyUiPython {
    param([string]$Root = $StudioEnv.STUDIO_COMFYUI_DIR)

    $candidates = @(
        (Join-Path $Root 'python_embeded\python.exe'),
        (Join-Path $Root '.venv\Scripts\python.exe'),
        (Join-Path $Root 'venv\Scripts\python.exe')
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }

    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}
