# 合并分卷（PowerShell 版）
$d = Split-Path -Parent $MyInvocation.MyCommand.Path
$out = Join-Path $d "anima-studio-portable-20261031.zip"
if (Test-Path $out) { Remove-Item $out -Force }
Get-ChildItem $d -Filter "anima-studio-portable-20261031.zip.*" | Sort-Object Name | ForEach-Object {
    $b = [System.IO.File]::ReadAllBytes($_.FullName)
    $fs = [System.IO.File]::Open($out, [System.IO.FileMode]::Append)
    $fs.Write($b, 0, $b.Length); $fs.Close()
}
"merged -> $out"
(Get-FileHash $out -Algorithm SHA256).Hash
