# Costruisce l'eseguibile Windows (.exe). Da lanciare SU Windows (PowerShell).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip -q
pip install -r requirements.txt pyinstaller -q
if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist)  { Remove-Item -Recurse -Force dist }
pyinstaller HAL_Agent.spec --noconfirm
Write-Host "OK: vedi dist\HAL Agent.exe"
