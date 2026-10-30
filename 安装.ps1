# 安装.ps1 - anima-studio portable self-check
# ASCII only on purpose: Windows PowerShell 5.1 parses a BOM-less .ps1 as ANSI,
# so non-ASCII text in this file would corrupt the parser. Chinese notes live in
# the markdown docs next to this script.
#
# Usage:
#   .\安装.ps1
#   .\安装.ps1 -ComfyUIDir "D:\anima-studio\ComfyUI"
#
# It only does three things: write user-level env vars, check ComfyUI / Python,
# and verify required weights. It downloads and installs nothing.

param(
    [string]$ComfyUIDir = "",
    [switch]$NoEnvWrite
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ok = $true

function Say([string]$text, [string]$level = "info") {
    $tag = "[ .. ]"
    if ($level -eq "ok")   { $tag = "[ OK ]" }
    if ($level -eq "warn") { $tag = "[WARN]" }
    if ($level -eq "fail") { $tag = "[FAIL]" }
    Write-Host "$tag $text"
}

Write-Host ""
Write-Host "anima-studio portable self-check"
Write-Host "root: $Root"
Write-Host ""

# ---------------------------------------------------------------- Python
Write-Host "-- python --"
$py = Get-Command python -ErrorAction SilentlyContinue
if ($py) {
    $ver = (& python -c "import sys;print('%d.%d.%d'%sys.version_info[:3])") 2>$null
    Say "python $ver ($($py.Source))"
    if ($ver -notmatch '^3\.(1[0-9])\.') { Say "3.11+ recommended; tested on 3.12.10" "warn" }
} else {
    Say "python not found; install with: winget install Python.Python.3.12" "fail"
    $ok = $false
}

# ---------------------------------------------------------------- ComfyUI
Write-Host ""
Write-Host "-- comfyui --"
if (-not $ComfyUIDir) { $ComfyUIDir = $env:STUDIO_COMFYUI_DIR }
if (-not $ComfyUIDir) { $ComfyUIDir = Join-Path $Root "ComfyUI" }

if (Test-Path (Join-Path $ComfyUIDir "main.py")) {
    Say "ComfyUI at $ComfyUIDir"
    $venvPy = Join-Path $ComfyUIDir ".venv\Scripts\python.exe"
    $verFile = Join-Path $ComfyUIDir "comfyui_version.py"
    if (Test-Path $verFile) {
        $m = Select-String -Path $verFile -Pattern '__version__\s*=\s*["'']([^"'']+)' -ErrorAction SilentlyContinue
        if ($m) { Say "comfyui version $($m.Matches[0].Groups[1].Value)" }
    }
    if (Test-Path $venvPy) {
        try {
            $torch = (& $venvPy -c "import torch;print(torch.__version__, torch.version.cuda, torch.cuda.is_available())" 2>&1 | Out-String).Trim()
            if ($LASTEXITCODE -eq 0) { Say "torch $torch" } else { Say "torch import failed: $torch" "warn" }
        } catch {
            Say "torch check skipped: $($_.Exception.Message)" "warn"
        }
    } else {
        Say "missing $venvPy ; create the venv first (see docs section 5)" "warn"
    }
    $nodes = @("ComfyUI-Manager", "ComfyUI-GGUF", "ComfyUI-VideoHelperSuite", "ComfyUI-KJNodes", "ComfyUI-Impact-Pack")
    $missing = @()
    foreach ($n in $nodes) {
        if (-not (Test-Path (Join-Path $ComfyUIDir "custom_nodes\$n"))) { $missing += $n }
    }
    if ($missing.Count -gt 0) {
        Say ("missing custom nodes: " + ($missing -join ", ")) "warn"
    } else {
        Say "custom nodes 5/5 present"
    }
} else {
    Say "no main.py under $ComfyUIDir" "fail"
    Say "clone ComfyUI first; see docs section 5" "warn"
    $ok = $false
}

# ---------------------------------------------------------------- env vars
Write-Host ""
Write-Host "-- environment --"
$vars = @{
    STUDIO_PROJECT_DIR  = (Join-Path $Root "project")
    STUDIO_COMFYUI_DIR  = $ComfyUIDir
    STUDIO_COMFYUI_HOST = "127.0.0.1:8188"
    STUDIO_CONSOLE_HOST = "127.0.0.1"
    STUDIO_CONSOLE_PORT = "8766"
    STUDIO_MIN_COMFYUI  = "0.3.31"
    STUDIO_OLLAMA_HOST  = "127.0.0.1:11434"
}
foreach ($k in $vars.Keys) {
    if ($NoEnvWrite) {
        Set-Item -Path "Env:\$k" -Value $vars[$k]
        Say "$k = $($vars[$k]) (this session only)"
    } else {
        [Environment]::SetEnvironmentVariable($k, $vars[$k], "User")
        Set-Item -Path "Env:\$k" -Value $vars[$k]
        Say "$k = $($vars[$k]) (user level)"
    }
}

# ---------------------------------------------------------------- weights
Write-Host ""
Write-Host "-- required weights --"
$checks = @(
    @{ p = "diffusion_models\anima-base-v1.0.safetensors"; min = 4140396144 },
    @{ p = "text_encoders\qwen_3_06b_base.safetensors";   min = 1180213745 },
    @{ p = "vae\qwen_image_vae.safetensors";              min = 251268184 }
)
$modelsDir = Join-Path $ComfyUIDir "models"
foreach ($c in $checks) {
    $f = Join-Path $modelsDir $c.p
    if ((Test-Path $f) -and ((Get-Item $f).Length -ge $c.min)) {
        Say ("{0} = {1:N2} GB" -f $c.p, ((Get-Item $f).Length / 1GB))
    } else {
        Say ("{0} missing or too small (expect >= {1:N2} GB)" -f $c.p, ($c.min / 1GB)) "fail"
        $ok = $false
    }
}

Write-Host ""
if ($ok) {
    Write-Host "self-check passed." -ForegroundColor Green
    Write-Host "next:  .\start.cmd   console at http://127.0.0.1:8766"
    Write-Host "       .\audit.cmd   audit only, no server"
} else {
    Write-Host "some checks failed; fix the items above and re-run." -ForegroundColor Yellow
    exit 1
}
