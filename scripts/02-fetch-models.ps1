# 02-fetch-models.ps1 - verify the weights listed in config/models.manifest.json.
#
# The manifest marks every entry auto_downloadable=false: there is no verified
# public CDN for these files, so this script only reports what is present and
# prints the exact destination for what is missing. Never a silent no-op.
#
# Exit code 0 = all required weights present, 1 = something is missing.

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $ProjectRoot 'config\studio-env.ps1')
Import-StudioEnv

$manifest = Get-Content (Join-Path $ProjectRoot 'config\models.manifest.json') -Raw | ConvertFrom-Json
$missing = 0

foreach ($f in $manifest.files) {
    $dest = Join-Path $StudioEnv.STUDIO_COMFYUI_DIR "models\$($f.target)\$($f.name)"
    if (Test-Path $dest) {
        $len = (Get-Item $dest).Length
        if ($len -lt $f.min_bytes) {
            Write-Host "[warn] too small: $($f.name) $len < $($f.min_bytes) (truncated download?)"
            $missing++
        } else {
            Write-Host "[ok] cached: $($f.name) $([math]::Round($len / 1GB, 2)) GB"
        }
    } else {
        Write-Host "[warn] missing: $($f.name)"
        Write-Host "[info]   place it at: $dest"
        Write-Host "[info]   source     : $($f.source_hint)"
        Write-Host "[info]   license    : $($f.license)"
        $missing++
    }
}

foreach ($f in $manifest.optional) {
    $dest = Join-Path $StudioEnv.STUDIO_COMFYUI_DIR "models\$($f.target)\$($f.name)"
    if (Test-Path $dest) {
        Write-Host "[ok] optional present: $($f.name)"
    } else {
        Write-Host "[info] optional absent: $($f.name) (stages: $($f.required_for -join ', '))"
    }
}

if ($missing -gt 0) {
    Write-Host "[warn] $missing required file(s) missing - stage 0 gate stays closed"
    exit 1
}
Write-Host "[ok] all required weights present"
exit 0
