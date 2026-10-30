# 10-start-comfyui.ps1 - start ComfyUI on the loopback port and wait until ready.
# Idempotent: returns immediately with exit 0 when the port is already served.
#
# ComfyUI is launched from main.py (embedded python when present) so stdout and
# stderr can be redirected to logs\. Loopback only: never pass --listen 0.0.0.0.

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $ProjectRoot 'config\studio-env.ps1')
Import-StudioEnv

$Root = $StudioEnv.STUDIO_COMFYUI_DIR
$Host_ = $StudioEnv.STUDIO_COMFYUI_HOST
$Port = [int]($Host_ -split ':' | Select-Object -Last 1)
$Base = "http://$Host_"

$OutLog = Join-Path $ProjectRoot 'logs\comfyui.out.log'
$ErrLog = Join-Path $ProjectRoot 'logs\comfyui.err.log'
New-Item -ItemType Directory -Path (Join-Path $ProjectRoot 'logs') -Force | Out-Null

# Start-Process always truncates its redirection targets, so rotate first:
# without this every restart destroys the previous run's evidence.
foreach ($lf in @($OutLog, $ErrLog)) {
    if (Test-Path -LiteralPath $lf) {
        $prev = $lf -replace '\.log$', '.prev.log'
        Move-Item -LiteralPath $lf -Destination $prev -Force
    }
}

function Test-ComfyUi {
    param([string]$Url)
    try {
        Invoke-RestMethod -Uri "$Url/system_stats" -TimeoutSec 3 | Out-Null
        return $true
    } catch { return $false }
}

if (Test-ComfyUi -Url $Base) {
    Write-Host "[ok] ComfyUI already serving on $Host_"
    exit 0
}

$Launcher = Get-ComfyUiLauncher -Root $Root
if (-not $Launcher) {
    Write-Error "ComfyUI not found under $Root - install it or set STUDIO_COMFYUI_DIR"
    exit 1
}

$Py = Get-ComfyUiPython -Root $Root
if (-not $Py) {
    Write-Error "no python interpreter available for ComfyUI"
    exit 1
}

Write-Host "[..] starting ComfyUI from $Launcher"
if ($Launcher -like '*.py') {
    $proc = Start-Process -FilePath $Py -ArgumentList @("`"$Launcher`"", '--port', $Port, '--listen', '127.0.0.1') `
        -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog `
        -WindowStyle Hidden -PassThru -WorkingDirectory $Root
} else {
    $proc = Start-Process -FilePath $Launcher -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog -WindowStyle Hidden -PassThru -WorkingDirectory $Root
}
Write-Host "[ok] pid=$($proc.Id) out=$OutLog"

$ready = $false
for ($i = 0; $i -lt 180; $i++) {
    Start-Sleep -Seconds 1
    if (Test-ComfyUi -Url $Base) { $ready = $true; break }
}

if (-not $ready) {
    Write-Error "ComfyUI not responding on $Host_ after 180s; see $ErrLog"
    exit 1
}
Write-Host "[ok] ComfyUI ready on $Base"
exit 0
