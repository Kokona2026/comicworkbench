# checks.ps1 - read-only forensics helper for the audit engine.
# Emits exactly one JSON value on stdout. ASCII-only output by design.
#
# Usage: powershell -File app\checks.ps1 -Mode <env|ports|syntax|ascii|disk|processes|gpu>

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('env', 'ports', 'syntax', 'ascii', 'disk', 'processes', 'gpu')]
    [string]$Mode
)

$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

. (Join-Path $ProjectRoot 'config\studio-env.ps1')

$ComfyPort = [int]($StudioEnv.STUDIO_COMFYUI_HOST -split ':' | Select-Object -Last 1)
$ConsolePort = [int]$StudioEnv.STUDIO_CONSOLE_PORT

switch ($Mode) {

    'env' {
        $rows = [ordered]@{}
        foreach ($key in @('STUDIO_PROJECT_DIR', 'STUDIO_COMFYUI_DIR',
                           'STUDIO_COMFYUI_HOST', 'STUDIO_CONSOLE_HOST', 'STUDIO_CONSOLE_PORT')) {
            $val = [Environment]::GetEnvironmentVariable($key, 'User')
            if (-not $val) { $val = [Environment]::GetEnvironmentVariable($key, 'Process') }
            $rows[$key] = $val
        }
        $comfy = $StudioEnv.STUDIO_COMFYUI_DIR
        $launcher = Get-ComfyUiLauncher -Root $comfy
        $py = Get-ComfyUiPython -Root $comfy
        $rows['COMFYUI_DIR_EXISTS'] = [bool](Test-Path $comfy)
        $rows['COMFYUI_LAUNCHER'] = [string]$launcher
        $rows['COMFYUI_PYTHON'] = [string]$py
        $rows['PYTHON_VERSION'] = [string](& python -c "import platform,sys;sys.stdout.write(platform.python_version())" 2>$null)
        $rows['CONDA_OR_VENV'] = [bool]$env:VIRTUAL_ENV
        ConvertTo-Json -InputObject $rows -Compress
    }

    'ports' {
        $rows = @()
        foreach ($p in @($ComfyPort, $ConsolePort)) {
            $conns = Get-NetTCPConnection -State Listen -LocalPort $p
            foreach ($c in $conns) {
                $proc = Get-Process -Id $c.OwningProcess
                $cmd = ''
                $wmi = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)"
                if ($wmi) { $cmd = [string]$wmi.CommandLine }
                $rows += [ordered]@{
                    port       = $p
                    address    = $c.LocalAddress
                    pid        = $c.OwningProcess
                    process    = $proc.ProcessName
                    cmd        = $cmd
                    loopback   = ($c.LocalAddress -eq '127.0.0.1' -or $c.LocalAddress -eq '::1')
                }
            }
        }
        ConvertTo-Json -InputObject @($rows) -Compress
    }

    'syntax' {
        $rows = @()
        $files = @()
        $files += Get-ChildItem -Path (Join-Path $ProjectRoot 'scripts') -Filter '*.ps1' -File
        $files += Get-ChildItem -Path (Join-Path $ProjectRoot 'config') -Filter '*.ps1' -File
        $files += Get-ChildItem -Path $ScriptDir -Filter '*.ps1' -File
        foreach ($f in $files) {
            $tokens = $null
            $errs = $null
            [System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$tokens, [ref]$errs) | Out-Null
            $rows += [ordered]@{
                file    = $f.Name
                ok      = ($errs.Count -eq 0)
                message = if ($errs.Count -gt 0) { [string]$errs[0].Message } else { '' }
            }
        }
        ConvertTo-Json -InputObject @($rows) -Compress
    }

    'ascii' {
        $rows = @()
        $files = @()
        $files += Get-ChildItem -Path (Join-Path $ProjectRoot 'scripts') -Filter '*.ps1' -File
        $files += Get-ChildItem -Path (Join-Path $ProjectRoot 'config') -Filter '*.ps1' -File
        $files += Get-ChildItem -Path $ScriptDir -Filter '*.ps1' -File
        foreach ($f in $files) {
            $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
            $bad = 0
            foreach ($b in $bytes) { if ($b -gt 127) { $bad++ } }
            if ($bad -gt 0) {
                $rows += [ordered]@{ file = $f.Name; ok = $false; non_ascii = $bad }
            }
        }
        ConvertTo-Json -InputObject @($rows) -Compress
    }

    'disk' {
        $drive = (Get-Item $ProjectRoot).PSDrive.Name
        $d = Get-PSDrive -Name $drive
        $rows = [ordered]@{
            drive      = $drive
            free_gb    = [math]::Round($d.Free / 1GB, 1)
            used_gb    = [math]::Round($d.Used / 1GB, 1)
        }
        ConvertTo-Json -InputObject $rows -Compress
    }

    'processes' {
        $rows = @()
        $names = @('python', 'pythonw', 'comfyui')
        $procs = Get-CimInstance Win32_Process -Filter "Name like '%python%'"
        foreach ($p in $procs) {
            $cmd = [string]$p.CommandLine
            # Match the ComfyUI entry point or its install path. A bare 'comfy'
            # also matches our own '--with-comfyui' switch, which made the
            # console look like a duplicate ComfyUI instance (SVC-04).
            if ($cmd -match 'main\.py' -or $cmd -match '[\\/]ComfyUI[\\/]') {
                $rows += [ordered]@{
                    pid    = $p.ProcessId
                    parent = $p.ParentProcessId
                    name   = $p.Name
                    cmd    = $cmd.Substring(0, [math]::Min(220, $cmd.Length))
                }
            }
        }
        ConvertTo-Json -InputObject @($rows) -Compress
    }

    'gpu' {
        $out = & nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,driver_version --format=csv,noheader,nounits 2>$null
        if (-not $out) { '{"present":false}' ; break }
        $line = ($out -split "`n")[0]
        $parts = $line -split ',' | ForEach-Object { $_.Trim() }
        $rows = [ordered]@{
            present   = $true
            name      = $parts[0]
            used_mb   = [int][math]::Round([double]$parts[1])
            total_mb  = [int][math]::Round([double]$parts[2])
            util      = [int][math]::Round([double]$parts[3])
            driver    = $parts[4]
        }
        ConvertTo-Json -InputObject $rows -Compress
    }
}
