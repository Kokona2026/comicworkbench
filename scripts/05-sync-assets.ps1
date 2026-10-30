#Requires -Version 5.1
<#
.SYNOPSIS
    Idempotent checker and downloader for ComfyUI weights and required custom nodes.
.DESCRIPTION
    Complies with docs/01-architecture.md and docs/05-audit.md:
    1. Pure 7-bit ASCII only (no BOM / ANSI encoding issues on Windows PowerShell 5.1).
    2. Uses 'return' statements exclusively so orchestrator scripts are never terminated.
    3. Loads paths and model manifest from config/studio-env.ps1 and config/models.manifest.json.
    4. Idempotent: existing files meeting min_bytes and existing node repos are skipped.
    5. Atomic downloads via .part temporary files with curl.exe resume (-C -) support.
.PARAMETER CheckOnly
    Verify models, launcher, and custom nodes without downloading or cloning anything.
.PARAMETER IncludeOptional
    Also download Stage 3-5 optional weights (MDL-04: anima-lllite-inpainting, wan2.2-i2v-a14b-q8).
.PARAMETER Include2DVariants
    Also download Anima-Turbo v1.1 and Anima-Aesthetic v1.1 for 2D/2.5D workflows.
.PARAMETER SkipNodes
    Skip checking or cloning ComfyUI custom node repositories.
.PARAMETER HfBaseUrl
    Base URL for Hugging Face downloads. Defaults to https://huggingface.co (can be set to https://hf-mirror.com).
#>
[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$IncludeOptional,
    [switch]$Include2DVariants,
    [switch]$SkipNodes,
    [string]$HfBaseUrl = "https://huggingface.co",
    [string]$PipMirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# 1. Resolve Project Root and Load config/studio-env.ps1
# ---------------------------------------------------------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$ConfigDir = Join-Path $ProjectRoot "config"
$EnvScript = Join-Path $ConfigDir "studio-env.ps1"
$ManifestPath = Join-Path $ConfigDir "models.manifest.json"

$StudioEnv = @{}
if (Test-Path -LiteralPath $EnvScript) {
    . $EnvScript
}

$ComfyDir = $null
if ($env:STUDIO_COMFYUI_DIR -and $env:STUDIO_COMFYUI_DIR.Trim().Length -gt 0) {
    $ComfyDir = $env:STUDIO_COMFYUI_DIR.Trim()
} elseif ($StudioEnv -is [hashtable] -and $StudioEnv.ContainsKey("ComfyUiDir") -and $StudioEnv["ComfyUiDir"]) {
    $ComfyDir = [string]$StudioEnv["ComfyUiDir"]
} elseif ($StudioEnv -is [hashtable] -and $StudioEnv.ContainsKey("STUDIO_COMFYUI_DIR") -and $StudioEnv["STUDIO_COMFYUI_DIR"]) {
    $ComfyDir = [string]$StudioEnv["STUDIO_COMFYUI_DIR"]
} else {
    $ComfyDir = Join-Path $ProjectRoot "ComfyUI"
}

$HfBaseUrl = $HfBaseUrl.TrimEnd('/')

# curl.exe does not read the WinINET proxy that the rest of Windows uses, while
# Python's urllib does. Without this, HF is unreachable from curl on a machine
# behind a local proxy even though the same URL works from Python.
$ProxyUrl = $null
foreach ($pKey in @("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")) {
    $pVal = [Environment]::GetEnvironmentVariable($pKey)
    if ($pVal -and $pVal.Trim().Length -gt 0) { $ProxyUrl = $pVal.Trim(); break }
}
if (-not $ProxyUrl) {
    $IeSettings = Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" -ErrorAction SilentlyContinue
    if ($IeSettings -and $IeSettings.ProxyEnable -eq 1 -and $IeSettings.ProxyServer) {
        $ProxyRaw = [string]$IeSettings.ProxyServer
        if ($ProxyRaw -match "=") {
            foreach ($pPart in ($ProxyRaw -split ";")) {
                if ($pPart -match "^https?=(.+)$") { $ProxyRaw = $Matches[1]; break }
            }
        }
        if ($ProxyRaw -notmatch "^[a-zA-Z]+://") { $ProxyRaw = "http://$ProxyRaw" }
        $ProxyUrl = $ProxyRaw
    }
}


function Write-Step {
    param([string]$Tag, [string]$Message, [string]$Color = "Cyan")
    Write-Host ("[{0}] {1}" -f $Tag, $Message) -ForegroundColor $Color
}

# ---------------------------------------------------------------------------
# 2. Verify ComfyUI Installation & Launcher (ENV-04)
# ---------------------------------------------------------------------------
Write-Step "INFO" ("ProjectRoot : {0}" -f $ProjectRoot) "Gray"
Write-Step "INFO" ("ComfyUI Dir : {0}" -f $ComfyDir) "Gray"
$ProxyLabel = "(direct)"
if ($ProxyUrl) { $ProxyLabel = $ProxyUrl }
Write-Step "INFO" ("Proxy       : {0}" -f $ProxyLabel) "Gray"

$ComfyMainPy = Join-Path $ComfyDir "main.py"
$PortableMainPy = Join-Path (Join-Path $ComfyDir "ComfyUI") "main.py"
$ActualComfyRoot = $ComfyDir

if (Test-Path -LiteralPath $ComfyMainPy) {
    $ActualComfyRoot = $ComfyDir
} elseif (Test-Path -LiteralPath $PortableMainPy) {
    $ActualComfyRoot = Join-Path $ComfyDir "ComfyUI"
} else {
    Write-Step "FAIL" ("ENV-04: ComfyUI main.py not found under {0}" -f $ComfyDir) "Red"
    Write-Step "HINT" "1. Download ComfyUI Portable from: https://github.com/comfy-org/ComfyUI/releases" "Yellow"
    Write-Step "HINT" ("2. Extract to '{0}' or run: setx STUDIO_COMFYUI_DIR ""D:\Path\To\ComfyUI""" -f $ComfyDir) "Yellow"
    if (-not $CheckOnly) {
        return [pscustomobject]@{
            ok          = $false
            stage       = "ENV-04"
            comfyui_dir = $ComfyDir
            message     = "ComfyUI installation missing. Install ComfyUI first before syncing weights and nodes."
        }
    }
}

$ModelsRoot = Join-Path $ActualComfyRoot "models"
$CustomNodesRoot = Join-Path $ActualComfyRoot "custom_nodes"

# Locate the interpreter that owns ComfyUI's dependencies.
# config/studio-env.ps1 already owns this rule (python_embeded -> .venv -> venv
# -> PATH), so ask it first instead of duplicating the order here.
$ComfyPython = $null
if (Get-Command "Get-ComfyUiPython" -ErrorAction SilentlyContinue) {
    $ComfyPython = Get-ComfyUiPython -Root $ActualComfyRoot
}
if (-not $ComfyPython) {
    $EmbeddedPyCandidates = @(
        (Join-Path (Split-Path -Parent $ActualComfyRoot) "python_embeded\python.exe"),
        (Join-Path $ComfyDir "python_embeded\python.exe"),
        (Join-Path $ActualComfyRoot ".venv\Scripts\python.exe"),
        (Join-Path $ActualComfyRoot "venv\Scripts\python.exe")
    )
    foreach ($cand in $EmbeddedPyCandidates) {
        if ($cand -and (Test-Path -LiteralPath $cand)) {
            $ComfyPython = $cand
            break
        }
    }
}
if ($ComfyPython) { Write-Step "INFO" ("ComfyPython : {0}" -f $ComfyPython) "Gray" }

# ---------------------------------------------------------------------------
# 3. Canonical Model Registry (Aligned with MDL-01..MDL-04 & 2D/2.5D Guide)
# ---------------------------------------------------------------------------
$DefaultModelSpecs = @(
    [pscustomobject]@{
        id        = "MDL-01"
        name      = "anima-base-v1.0.safetensors"
        rel_dir   = "diffusion_models"
        required  = $true
        category  = "required"
        min_bytes = 4000000000L
        repo_path = "circlestone-labs/Anima/resolve/main/split_files/diffusion_models/anima-base-v1.0.safetensors"
    },
    [pscustomobject]@{
        id        = "MDL-02"
        name      = "qwen_3_06b_base.safetensors"
        rel_dir   = "text_encoders"
        required  = $true
        category  = "required"
        min_bytes = 1000000000L
        repo_path = "circlestone-labs/Anima/resolve/main/split_files/text_encoders/qwen_3_06b_base.safetensors"
    },
    [pscustomobject]@{
        id        = "MDL-03"
        name      = "qwen_image_vae.safetensors"
        rel_dir   = "vae"
        required  = $true
        category  = "required"
        min_bytes = 200000000L
        repo_path = "circlestone-labs/Anima/resolve/main/split_files/vae/qwen_image_vae.safetensors"
    },
    [pscustomobject]@{
        id        = "MDL-04A"
        name      = "anima-lllite-inpainting.safetensors"
        rel_dir   = "controlnet"
        required  = $false
        category  = "optional"
        min_bytes = 50000000L
        repo_path = "kohya-ss/Anima-LLLite/resolve/main/anima-lllite-inpainting-v2.safetensors"
    },
    [pscustomobject]@{
        id        = "MDL-04B"
        name      = "wan2.2-i2v-a14b-q8.gguf"
        rel_dir   = "unet"
        required  = $false
        category  = "optional"
        min_bytes = 14000000000L
        repo_path = "QuantStack/Wan2.2-I2V-A14B-GGUF/resolve/main/HighNoise/Wan2.2-I2V-A14B-HighNoise-Q8_0.gguf"
    },
    [pscustomobject]@{
        id        = "MDL-2D-TURBO"
        name      = "anima-turbo-v1.1.safetensors"
        rel_dir   = "diffusion_models"
        required  = $false
        category  = "variant_2d"
        min_bytes = 4000000000L
        repo_path = "circlestone-labs/Anima/resolve/main/split_files/diffusion_models/anima-turbo-v1.1.safetensors"
    },
    [pscustomobject]@{
        id        = "MDL-25D-AESTHETIC"
        name      = "anima-aesthetic-v1.1.safetensors"
        rel_dir   = "diffusion_models"
        required  = $false
        category  = "variant_2d"
        min_bytes = 4000000000L
        repo_path = "circlestone-labs/Anima/resolve/main/split_files/diffusion_models/anima-aesthetic-v1.1.safetensors"
    }
)

# Merge overrides from config/models.manifest.json if present.
# The manifest is the single source of truth: it may correct min_bytes, the
# target subdirectory, or the download URL of any entry above.
$ManifestNodes = @()
if (Test-Path -LiteralPath $ManifestPath) {
    try {
        $RawManifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $ManifestItems = @()
        if ($RawManifest -is [System.Array]) {
            $ManifestItems = $RawManifest
        } else {
            # v1 manifests used "models"; v2 splits required and optional files.
            foreach ($prop in @("files", "optional", "models")) {
                if ($RawManifest.PSObject.Properties.Name -contains $prop) {
                    $ManifestItems += @($RawManifest.$prop)
                }
            }
            if ($RawManifest.PSObject.Properties.Name -contains "custom_nodes") {
                $ManifestNodes = @($RawManifest.custom_nodes)
            }
        }
        foreach ($mItem in $ManifestItems) {
            $mName = $null
            if ($mItem.PSObject.Properties.Name -contains "name") { $mName = $mItem.name }
            elseif ($mItem.PSObject.Properties.Name -contains "filename") { $mName = $mItem.filename }
            elseif ($mItem.PSObject.Properties.Name -contains "file") { $mName = $mItem.file }
            if (-not $mName) { continue }

            foreach ($spec in $DefaultModelSpecs) {
                if ($spec.name -ne $mName) { continue }

                if ($mItem.PSObject.Properties.Name -contains "min_bytes" -and $mItem.min_bytes) {
                    $spec.min_bytes = [int64]$mItem.min_bytes
                } elseif ($mItem.PSObject.Properties.Name -contains "expected_bytes" -and $mItem.expected_bytes) {
                    $spec.min_bytes = [int64]([double]$mItem.expected_bytes * 0.99)
                }

                $DirValue = $null
                if ($mItem.PSObject.Properties.Name -contains "target" -and $mItem.target) {
                    $DirValue = [string]$mItem.target
                } elseif ($mItem.PSObject.Properties.Name -contains "dir" -and $mItem.dir) {
                    $DirValue = [string]$mItem.dir
                }
                if ($DirValue) { $spec.rel_dir = ($DirValue -replace '^models[\\/]', '') }

                if ($mItem.PSObject.Properties.Name -contains "url" -and $mItem.url) {
                    $spec | Add-Member -NotePropertyName "custom_url" -NotePropertyValue ([string]$mItem.url) -Force
                }
            }
        }
        Write-Step "INFO" ("Loaded manifest overrides from {0} ({1} entries)" -f $ManifestPath, $ManifestItems.Count) "Gray"
    } catch {
        Write-Step "WARN" ("Could not parse models.manifest.json, using default specs: {0}" -f $_.Exception.Message) "Yellow"
    }
}

# ---------------------------------------------------------------------------
# 4. Helper: Idempotent File Download with Resume & Size Verification
# ---------------------------------------------------------------------------
function Sync-ModelFile {
    param([pscustomobject]$Spec, [switch]$DryRun)

    $TargetDir = Join-Path $ModelsRoot $Spec.rel_dir
    $TargetFile = Join-Path $TargetDir $Spec.name
    $PartFile = "{0}.part" -f $TargetFile

    $DownloadUrl = "{0}/{1}" -f $HfBaseUrl, $Spec.repo_path
    if ($Spec.PSObject.Properties.Name -contains "custom_url" -and $Spec.custom_url) {
        $DownloadUrl = [string]$Spec.custom_url
    }

    if (Test-Path -LiteralPath $TargetFile) {
        $ActualSize = (Get-Item -LiteralPath $TargetFile).Length
        if ($ActualSize -ge $Spec.min_bytes) {
            Write-Step "PASS" ("[{0}] {1} present ({2:N2} GB)" -f $Spec.id, $Spec.name, ($ActualSize / 1GB)) "Green"
            return [pscustomobject]@{ id = $Spec.id; name = $Spec.name; status = "present"; path = $TargetFile }
        } else {
            Write-Step "WARN" ("[{0}] {1} incomplete ({2} < {3} bytes)" -f $Spec.id, $Spec.name, $ActualSize, $Spec.min_bytes) "Yellow"
        }
    } else {
        $SevColor = if ($Spec.required) { "Red" } else { "Yellow" }
        Write-Step "MISS" ("[{0}] {1} missing -> models/{2}/{1}" -f $Spec.id, $Spec.name, $Spec.rel_dir) $SevColor
    }

    if ($DryRun) {
        return [pscustomobject]@{ id = $Spec.id; name = $Spec.name; status = "missing"; path = $TargetFile; url = $DownloadUrl }
    }

    if (-not (Test-Path -LiteralPath $TargetDir)) {
        New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
    }

    Write-Step "DOWN" ("Downloading {0} from {1} ..." -f $Spec.name, $DownloadUrl) "Cyan"

    # Preferred path: app/fetch_assets.py. It uses 8 concurrent range requests
    # (single-stream curl measured ~0.5-0.9 MB/s here, 8 streams reach ~4 MB/s),
    # resumes per segment, and only renames after the byte count matches.
    $Fetcher = Join-Path $ProjectRoot "app\fetch_assets.py"
    $PyCmd = Get-Command "python" -ErrorAction SilentlyContinue
    if ((Test-Path -LiteralPath $Fetcher) -and ($null -ne $PyCmd)) {
        $FetchArgs = @($Fetcher, "--url", $DownloadUrl, "--dest", $TargetFile,
                       "--min-bytes", [string]$Spec.min_bytes, "--segments", "8")
        if ($ProxyUrl) { $FetchArgs += @("--proxy", $ProxyUrl) }
        & $PyCmd.Source @FetchArgs
        if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $TargetFile)) {
            $FinalSize = (Get-Item -LiteralPath $TargetFile).Length
            if ($FinalSize -ge $Spec.min_bytes) {
                Write-Step "PASS" ("[{0}] {1} downloaded and verified ({2:N2} GB)" -f $Spec.id, $Spec.name, ($FinalSize / 1GB)) "Green"
                return [pscustomobject]@{ id = $Spec.id; name = $Spec.name; status = "downloaded"; path = $TargetFile }
            }
        }
        Write-Step "WARN" ("python fetcher failed for {0}; retrying with curl.exe" -f $Spec.name) "Yellow"
    }

    $CurlExe = Get-Command "curl.exe" -ErrorAction SilentlyContinue
    if ($null -ne $CurlExe) {
        $CurlArgs = @("-L", "--fail", "--retry", "3", "--retry-delay", "5",
                      "--connect-timeout", "30", "-C", "-", "-o", $PartFile, $DownloadUrl)
        if ($ProxyUrl) { $CurlArgs = @("--proxy", $ProxyUrl) + $CurlArgs }
        & $CurlExe.Source @CurlArgs
        if ($LASTEXITCODE -ne 0) {
            Write-Step "FAIL" ("curl.exe failed for {0} (return code {1})" -f $Spec.name, $LASTEXITCODE) "Red"
            return [pscustomobject]@{ id = $Spec.id; name = $Spec.name; status = "error"; path = $TargetFile; url = $DownloadUrl }
        }
    } else {
        try {
            Invoke-WebRequest -Uri $DownloadUrl -OutFile $PartFile -UseBasicParsing
        } catch {
            Write-Step "FAIL" ("Download failed for {0}: {1}" -f $Spec.name, $_.Exception.Message) "Red"
            return [pscustomobject]@{ id = $Spec.id; name = $Spec.name; status = "error"; path = $TargetFile; url = $DownloadUrl }
        }
    }

    if (-not (Test-Path -LiteralPath $PartFile)) {
        Write-Step "FAIL" ("Temporary file not created for {0}" -f $Spec.name) "Red"
        return [pscustomobject]@{ id = $Spec.id; name = $Spec.name; status = "error"; path = $TargetFile; url = $DownloadUrl }
    }

    $DownloadedSize = (Get-Item -LiteralPath $PartFile).Length
    if ($DownloadedSize -lt $Spec.min_bytes) {
        Write-Step "FAIL" ("Downloaded file {0} is too small ({1} < {2} bytes)" -f $Spec.name, $DownloadedSize, $Spec.min_bytes) "Red"
        return [pscustomobject]@{ id = $Spec.id; name = $Spec.name; status = "error"; path = $TargetFile; url = $DownloadUrl }
    }

    Move-Item -LiteralPath $PartFile -Destination $TargetFile -Force
    Write-Step "PASS" ("[{0}] {1} downloaded and verified ({2:N2} GB)" -f $Spec.id, $Spec.name, ($DownloadedSize / 1GB)) "Green"
    return [pscustomobject]@{ id = $Spec.id; name = $Spec.name; status = "downloaded"; path = $TargetFile }
}

# ---------------------------------------------------------------------------
# 5. Execute Model Check / Sync
# ---------------------------------------------------------------------------
$ModelResults = @()
foreach ($spec in $DefaultModelSpecs) {
    $shouldProcess = $false
    if ($spec.category -eq "required") {
        $shouldProcess = $true
    } elseif ($spec.category -eq "optional" -and ($IncludeOptional -or $CheckOnly)) {
        $shouldProcess = $true
    } elseif ($spec.category -eq "variant_2d" -and ($Include2DVariants -or $CheckOnly)) {
        $shouldProcess = $true
    }

    if ($shouldProcess) {
        $res = Sync-ModelFile -Spec $spec -DryRun:$CheckOnly
        $ModelResults += $res
    }
}

# ---------------------------------------------------------------------------
# 6. Check & Clone Required Custom Nodes (Stage 0 Step 4)
# ---------------------------------------------------------------------------
$RequiredNodes = @(
    [pscustomobject]@{
        name = "ComfyUI-Manager"
        url  = "https://github.com/ltdrdata/ComfyUI-Manager.git"
        desc = "Node manager for ComfyUI"
    },
    [pscustomobject]@{
        name = "ComfyUI-GGUF"
        url  = "https://github.com/city96/ComfyUI-GGUF.git"
        desc = "GGUF loader for Wan 2.2 I2V A14B"
    },
    [pscustomobject]@{
        name = "ComfyUI-KJNodes"
        url  = "https://github.com/kijai/ComfyUI-KJNodes.git"
        desc = "Mask, image, and quality-of-life nodes"
    },
    [pscustomobject]@{
        name = "ComfyUI-VideoHelperSuite"
        url  = "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git"
        desc = "Video loading and combining for Stage 5"
    },
    [pscustomobject]@{
        name = "ComfyUI-Impact-Pack"
        url  = "https://github.com/ltdrdata/ComfyUI-Impact-Pack.git"
        desc = "FaceDetailer and regional detailing for Stage 3/4"
    }
)

# The manifest wins when it declares custom_nodes: one place to add a node pack.
if ($ManifestNodes.Count -gt 0) {
    $RequiredNodes = @($ManifestNodes | ForEach-Object {
        $Desc = [string]$_.name
        if ($_.PSObject.Properties.Name -contains "desc" -and $_.desc) { $Desc = [string]$_.desc }
        elseif ($_.PSObject.Properties.Name -contains "purpose" -and $_.purpose) { $Desc = [string]$_.purpose }
        [pscustomobject]@{ name = [string]$_.name; url = [string]$_.url; desc = $Desc }
    })
    Write-Step "INFO" ("Custom node list from manifest: {0} pack(s)" -f $RequiredNodes.Count) "Gray"
}

$NodeResults = @()
if (-not $SkipNodes) {
    $GitCmd = Get-Command "git.exe" -ErrorAction SilentlyContinue
    foreach ($node in $RequiredNodes) {
        $NodeDir = Join-Path $CustomNodesRoot $node.name
        if (Test-Path -LiteralPath $NodeDir) {
            Write-Step "PASS" ("[NODE] {0} present" -f $node.name) "Green"
            $NodeResults += [pscustomobject]@{ name = $node.name; status = "present"; path = $NodeDir }
            continue
        }

        Write-Step "MISS" ("[NODE] {0} missing ({1})" -f $node.name, $node.desc) "Yellow"
        if ($CheckOnly) {
            $NodeResults += [pscustomobject]@{ name = $node.name; status = "missing"; path = $NodeDir }
            continue
        }

        if ($null -eq $GitCmd) {
            Write-Step "FAIL" "git.exe not found in PATH; cannot clone custom nodes automatically." "Red"
            $NodeResults += [pscustomobject]@{ name = $node.name; status = "error_no_git"; path = $NodeDir }
            continue
        }

        if (-not (Test-Path -LiteralPath $CustomNodesRoot)) {
            New-Item -ItemType Directory -Path $CustomNodesRoot -Force | Out-Null
        }

        Write-Step "GIT" ("Cloning {0} ..." -f $node.url) "Cyan"
        # git ignores the WinINET proxy as well; without -c http.proxy the clone
        # hangs forever on github.com behind a local proxy. GIT_TERMINAL_PROMPT=0
        # makes a bad credential prompt fail instead of wait.
        $GitArgs = @("clone", "--depth", "1", $node.url, $NodeDir)
        if ($ProxyUrl) {
            $GitArgs = @("-c", ("http.proxy={0}" -f $ProxyUrl),
                         "-c", ("https.proxy={0}" -f $ProxyUrl)) + $GitArgs
        }
        $env:GIT_TERMINAL_PROMPT = "0"
        & $GitCmd.Source @GitArgs
        if ($LASTEXITCODE -ne 0) {
            Write-Step "FAIL" ("git clone failed for {0}" -f $node.name) "Red"
            $NodeResults += [pscustomobject]@{ name = $node.name; status = "error_clone"; path = $NodeDir }
            continue
        }

        $ReqFile = Join-Path $NodeDir "requirements.txt"
        if ((Test-Path -LiteralPath $ReqFile) -and ($null -ne $ComfyPython)) {
            Write-Step "PIP" ("Installing requirements.txt for {0} ..." -f $node.name) "Cyan"
            & $ComfyPython -m pip install -q -i $PipMirror -r $ReqFile
            if ($LASTEXITCODE -ne 0) {
                Write-Step "WARN" ("pip install failed for {0}; the node may still load" -f $node.name) "Yellow"
            }
        }

        Write-Step "PASS" ("[NODE] {0} installed" -f $node.name) "Green"
        $NodeResults += [pscustomobject]@{ name = $node.name; status = "installed"; path = $NodeDir }
    }
}

# ---------------------------------------------------------------------------
# 7. Final Summary & Idempotent Return (Always use return)
# ---------------------------------------------------------------------------
$RequiredMissing = @(
    $ModelResults | Where-Object {
        ($_.id -in @("MDL-01", "MDL-02", "MDL-03")) -and ($_.status -notin @("present", "downloaded"))
    }
)
$NodesMissing = @(
    $NodeResults | Where-Object { $_.status -notin @("present", "installed") }
)

$AllReady = ($RequiredMissing.Count -eq 0) -and (Test-Path -LiteralPath (Join-Path $ActualComfyRoot "main.py"))

if ($AllReady) {
    Write-Step "DONE" "All required Stage 0 weights (3/3) and ComfyUI launcher are ready." "Green"
} else {
    Write-Step "WARN" ("Stage 0 incomplete: {0} required model(s) missing, {1} custom node(s) missing." -f $RequiredMissing.Count, $NodesMissing.Count) "Yellow"
}

return [pscustomobject]@{
    ok               = [bool]$AllReady
    comfyui_root     = $ActualComfyRoot
    launcher_present = [bool](Test-Path -LiteralPath (Join-Path $ActualComfyRoot "main.py"))
    models           = $ModelResults
    custom_nodes     = $NodeResults
}
