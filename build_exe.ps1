$ErrorActionPreference = "Stop"

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    python -m venv (Join-Path $PSScriptRoot ".venv")
}

& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $PSScriptRoot "requirements.txt") pyinstaller
$iconPath = Join-Path $PSScriptRoot "rvm.ico"
& $venvPython -m PyInstaller --clean --onefile --noconsole --icon $iconPath --add-data "$iconPath;." --name "RobloxVersionManager" (Join-Path $PSScriptRoot "app.py")

Write-Host "Built executable: dist\RobloxVersionManager.exe"
