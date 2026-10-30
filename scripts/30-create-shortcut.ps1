<#
.SYNOPSIS
    Create a desktop shortcut for the studio console, with a generated icon.

.DESCRIPTION
    Idempotent. Renders app/static/studio.ico from the same mark the web UI uses
    (rounded gradient square + sparkle), then writes a shortcut on the current
    user's desktop that starts start.cmd in the project root.

    Conventions honoured here:
      * config/ stays the single source of truth - this script only reads paths
        from the repo layout, no tunables are inlined.
      * ASCII-only source, because Windows PowerShell 5.1 reads BOM-less files
        as ANSI. The visible shortcut label is therefore built from Unicode
        code points instead of literal CJK characters.
      * Re-runnable: existing files are reused unless -Force is given.

.PARAMETER Force
    Regenerate the icon and overwrite the shortcut even if they already exist.

.PARAMETER IconOnly
    Only (re)generate the icon; do not touch the desktop.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\30-create-shortcut.ps1

.EXAMPLE
    .\scripts\30-create-shortcut.ps1 -Force -IconOnly
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$IconOnly,
    [string]$Label,
    [string]$Description = "Anima Studio - local ComfyUI console"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$staticDir = Join-Path $root "app\static"
$iconPath = Join-Path $staticDir "studio.ico"
$previewPath = Join-Path $staticDir "studio-256.png"
$target = Join-Path $root "start.cmd"

function New-RoundedRectPath {
    param(
        [single]$X, [single]$Y, [single]$W, [single]$H, [single]$Radius
    )
    $path = [System.Drawing.Drawing2D.GraphicsPath]::new()
    $d = [single]($Radius * 2)
    $path.AddArc($X, $Y, $d, $d, 180, 90)
    $path.AddArc(($X + $W - $d), $Y, $d, $d, 270, 90)
    $path.AddArc(($X + $W - $d), ($Y + $H - $d), $d, $d, 0, 90)
    $path.AddArc($X, ($Y + $H - $d), $d, $d, 90, 90)
    $path.CloseFigure()
    return $path
}

function New-StudioIcon {
    <#
        Draws one frame at a given pixel size. The geometry is authored on a
        256 grid and scaled, so the 16px frame is the same mark, not a guess.
    #>
    param([int]$Size)

    $bmp = [System.Drawing.Bitmap]::new($Size, $Size,
        [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.Clear([System.Drawing.Color]::Transparent)

    $radius = [single]([math]::Max(3.0, $Size * 0.27))
    $rectPath = New-RoundedRectPath -X 0 -Y 0 -W ([single]$Size) -H ([single]$Size) -Radius $radius

    $gradient = [System.Drawing.Drawing2D.LinearGradientBrush]::new(
        [System.Drawing.PointF]::new(0, 0),
        [System.Drawing.PointF]::new([single]$Size, [single]$Size),
        [System.Drawing.Color]::FromArgb(255, 91, 140, 255),
        [System.Drawing.Color]::FromArgb(255, 139, 92, 246))
    $g.FillPath($gradient, $rectPath)

    # Top-left sheen: keeps the square from reading as a flat block of colour.
    if ($Size -ge 32) {
        $sheen = [System.Drawing.Drawing2D.LinearGradientBrush]::new(
            [System.Drawing.PointF]::new(0, 0),
            [System.Drawing.PointF]::new([single]$Size, [single]($Size * 0.75)),
            [System.Drawing.Color]::FromArgb(56, 255, 255, 255),
            [System.Drawing.Color]::FromArgb(0, 255, 255, 255))
        $g.FillPath($sheen, $rectPath)
        $sheen.Dispose()
    }

    # Sparkle: same cubic outline as the inline SVG in app/static/index.html.
    $s = [single]($Size / 256.0)
    $star = [System.Drawing.Drawing2D.GraphicsPath]::new()
    $star.StartFigure()
    $star.AddBezier(
        [System.Drawing.PointF]::new(128 * $s, 36 * $s),
        [System.Drawing.PointF]::new(140.8 * $s, 78.4 * $s),
        [System.Drawing.PointF]::new(160.8 * $s, 98.4 * $s),
        [System.Drawing.PointF]::new(203.2 * $s, 111.2 * $s))
    $star.AddBezier(
        [System.Drawing.PointF]::new(203.2 * $s, 111.2 * $s),
        [System.Drawing.PointF]::new(160.8 * $s, 124 * $s),
        [System.Drawing.PointF]::new(140.8 * $s, 144 * $s),
        [System.Drawing.PointF]::new(128 * $s, 186.4 * $s))
    $star.AddBezier(
        [System.Drawing.PointF]::new(128 * $s, 186.4 * $s),
        [System.Drawing.PointF]::new(115.2 * $s, 144 * $s),
        [System.Drawing.PointF]::new(95.2 * $s, 124 * $s),
        [System.Drawing.PointF]::new(52.8 * $s, 111.2 * $s))
    $star.AddBezier(
        [System.Drawing.PointF]::new(52.8 * $s, 111.2 * $s),
        [System.Drawing.PointF]::new(95.2 * $s, 98.4 * $s),
        [System.Drawing.PointF]::new(115.2 * $s, 78.4 * $s),
        [System.Drawing.PointF]::new(128 * $s, 36 * $s))
    $star.CloseFigure()

    $white = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(242, 255, 255, 255))
    $g.FillPath($white, $star)

    # The satellite dot is noise below 32px, so small frames skip it.
    if ($Size -ge 32) {
        $dotR = [single](16.8 * $s)
        $dotBrush = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(184, 255, 255, 255))
        $g.FillEllipse($dotBrush, [single]((188 * $s) - $dotR), [single]((188 * $s) - $dotR),
            [single]($dotR * 2), [single]($dotR * 2))
        $dotBrush.Dispose()
    }

    $g.Dispose()
    $star.Dispose()
    $rectPath.Dispose()
    $gradient.Dispose()
    $white.Dispose()
    return $bmp
}

function Get-DibBytes {
    <#
        One ICO frame as a bottom-up 32bpp BMP (no file header). Small sizes
        must be DIB, not PNG: Explorer draws PNG frames, but GDI+ and many
        third-party shells decode only DIB and show noise otherwise.
    #>
    param([System.Drawing.Bitmap]$Bmp)

    $w = $Bmp.Width
    $h = $Bmp.Height
    $rect = [System.Drawing.Rectangle]::new(0, 0, $w, $h)
    $lock = $Bmp.LockBits($rect,
        [System.Drawing.Imaging.ImageLockMode]::ReadOnly,
        [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $stride = $lock.Stride
    $pixels = New-Object byte[] ($stride * $h)
    [System.Runtime.InteropServices.Marshal]::Copy($lock.Scan0, $pixels, 0, $pixels.Length)
    $Bmp.UnlockBits($lock)

    $ms = [System.IO.MemoryStream]::new()
    $bw = [System.IO.BinaryWriter]::new($ms)
    $bw.Write([UInt32]40)                  # biSize
    $bw.Write([Int32]$w)                   # biWidth
    $bw.Write([Int32]($h * 2))             # biHeight (image + mask)
    $bw.Write([UInt16]1)                   # biPlanes
    $bw.Write([UInt16]32)                  # biBitCount
    $bw.Write([UInt32]0)                   # biCompression: BI_RGB
    $bw.Write([UInt32]($w * $h * 4))       # biSizeImage
    $bw.Write([Int32]0)                    # biXPelsPerMeter
    $bw.Write([Int32]0)                    # biYPelsPerMeter
    $bw.Write([UInt32]0)                   # biClrUsed
    $bw.Write([UInt32]0)                   # biClrImportant
    for ($y = $h - 1; $y -ge 0; $y--) {
        $bw.Write($pixels, ($y * $stride), ($w * 4))
    }
    $maskStride = [int]([math]::Floor(($w + 31) / 32) * 4)
    $mask = New-Object byte[] ($maskStride * $h)
    $bw.Write($mask, 0, $mask.Length)      # all zero: alpha decides transparency
    $bw.Flush()

    $bytes = $ms.ToArray()
    $bw.Dispose()
    $ms.Dispose()
    return , $bytes
}

function Get-FrameBytes {
    param([System.Drawing.Bitmap]$Bmp, [int]$Size)
    if ($Size -le 64) { return , (Get-DibBytes -Bmp $Bmp) }
    $stream = [System.IO.MemoryStream]::new()
    $Bmp.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
    $bytes = $stream.ToArray()
    $stream.Dispose()
    return , $bytes
}

function Write-StudioIcon {
    param([string]$IcoPath, [string]$PngPath)

    Add-Type -AssemblyName System.Drawing

    $sizes = @(16, 24, 32, 48, 64, 128, 256)
    $frames = @()
    $preview = $null

    foreach ($size in $sizes) {
        $bmp = New-StudioIcon -Size $size
        $frame = Get-FrameBytes -Bmp $bmp -Size $size
        $frames += , $frame
        if ($size -eq 256) {
            $bmp.Save($PngPath, [System.Drawing.Imaging.ImageFormat]::Png)
            $preview = $bmp
        } else {
            $bmp.Dispose()
        }
    }
    if ($preview) { $preview.Dispose() }

    $out = [System.IO.MemoryStream]::new()
    $writer = [System.IO.BinaryWriter]::new($out)
    $writer.Write([UInt16]0)                 # reserved
    $writer.Write([UInt16]1)                 # type: icon
    $writer.Write([UInt16]$frames.Count)     # image count

    $offset = 6 + (16 * $frames.Count)
    for ($i = 0; $i -lt $frames.Count; $i++) {
        $size = $sizes[$i]
        $dim = if ($size -ge 256) { [byte]0 } else { [byte]$size }
        $writer.Write($dim)                  # width  (0 means 256)
        $writer.Write($dim)                  # height
        $writer.Write([byte]0)               # palette colours
        $writer.Write([byte]0)               # reserved
        $writer.Write([UInt16]1)             # planes
        $writer.Write([UInt16]32)            # bits per pixel
        $writer.Write([UInt32]$frames[$i].Length)
        $writer.Write([UInt32]$offset)
        $offset += $frames[$i].Length
    }
    foreach ($frame in $frames) { $writer.Write([byte[]]$frame) }
    $writer.Flush()
    [System.IO.File]::WriteAllBytes($IcoPath, $out.ToArray())
    $writer.Dispose()
    $out.Dispose()

    return (Get-Item $IcoPath).Length
}

# ---------------------------------------------------------------- icon
New-Item -ItemType Directory -Force -Path $staticDir | Out-Null
if ($Force -or -not (Test-Path $iconPath)) {
    $iconBytes = Write-StudioIcon -IcoPath $iconPath -PngPath $previewPath
    Write-Host ("[ok] icon  {0}  ({1:N0} bytes, 16-256 px)" -f $iconPath, $iconBytes)
} else {
    Write-Host ("[--] icon  {0}  (exists, use -Force to redraw)" -f $iconPath)
}

if ($IconOnly) { return }

# ---------------------------------------------------------------- shortcut
if (-not (Test-Path $target)) {
    throw "start.cmd not found: $target"
}

$desktop = [Environment]::GetFolderPath("Desktop")
if (-not $desktop -or -not (Test-Path $desktop)) {
    throw "Desktop folder not found for this user."
}

# Label: U+6F2B U+753B U+5DE5 U+4F5C U+53F0  (anime studio console)
if (-not $Label) {
    $Label = -join @([char]0x6F2B, [char]0x753B, [char]0x5DE5, [char]0x4F5C, [char]0x53F0)
}
$linkPath = Join-Path $desktop ($Label + ".lnk")

if ((Test-Path $linkPath) -and -not $Force) {
    Write-Host ("[--] link  {0}  (exists, use -Force to rewrite)" -f $linkPath)
    return
}

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($linkPath)
$link.TargetPath = $target
$link.WorkingDirectory = $root
$link.IconLocation = "$iconPath,0"
$link.WindowStyle = 1
$link.Description = $Description
$link.Save()

# Release the COM object so a later run can overwrite the file.
[void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
[System.GC]::Collect()

Write-Host ("[ok] link  {0}" -f $linkPath)
Write-Host ("     target {0}" -f $target)
Write-Host ("     icon   {0}" -f $iconPath)
