@echo off
REM Merge split parts back into one zip. Run inside this folder on Windows.
copy /b anima-studio-portable-20261031.zip.001+anima-studio-portable-20261031.zip.002+anima-studio-portable-20261031.zip.003+anima-studio-portable-20261031.zip.004 anima-studio-portable-20261031.zip >nul
echo Merged.
echo Expected SHA256: AEBE1844AAD93502FBF780556EB00DD0F28BF121877916BEC960B11694866E1C
certutil -hashfile anima-studio-portable-20261031.zip SHA256
pause
