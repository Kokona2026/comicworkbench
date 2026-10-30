# 11-stop-comfyui.ps1 - stop ComfyUI and release its VRAM.
#
# NOTE: use `return`, never `exit`: the console and future orchestration scripts
# may call this one and `exit` would terminate the caller too.

$ErrorActionPreference = 'SilentlyContinue'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $ProjectRoot 'config\studio-env.ps1')

$Port = [int]($StudioEnv.STUDIO_COMFYUI_HOST -split ':' | Select-Object -Last 1)

$stopped = 0

# Killing only the wrapper leaves the python child alive and holding VRAM, so
# resolve the owner of the listening socket first.
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port)
foreach ($c in $listeners) {
    $p = Get-Process -Id $c.OwningProcess
    if ($p -and $p.ProcessName -match '^(python|pythonw)$') {
        Write-Host "[..] stopping pid=$($p.Id) ($($p.ProcessName))"
        Stop-Process -Id $p.Id -Force
        $stopped++
    }
}

Get-Process -Name 'pythonw' -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "*$($StudioEnv.STUDIO_COMFYUI_DIR)*" } |
    ForEach-Object { Stop-Process -Id $_.Id -Force; $stopped++ }

for ($i = 0; $i -lt 20; $i++) {
    if (-not (Get-NetTCPConnection -State Listen -LocalPort $Port)) { break }
    Start-Sleep -Seconds 1
}

if ($stopped -eq 0) {
    Write-Host '[ok] no ComfyUI process running'
} else {
    Write-Host "[ok] stopped $stopped ComfyUI process(es), port $Port released"
}
return
