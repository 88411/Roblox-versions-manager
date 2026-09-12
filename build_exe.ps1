$ErrorActionPreference = "Stop"

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    python -m venv (Join-Path $PSScriptRoot ".venv")
}

& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $PSScriptRoot "requirements.txt") pyinstaller
$iconPath = Join-Path $PSScriptRoot "rvm.ico"
& $venvPython -m PyInstaller --clean --onefile --noconsole --icon $iconPath --add-data "$iconPath;." --name "RobloxVersionManager" --distpath (Join-Path $PSScriptRoot "dist-new") (Join-Path $PSScriptRoot "app.py")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE. Stop the old executable before rebuilding dist."
}
$newExe = Join-Path $PSScriptRoot "dist-new\RobloxVersionManager.exe"
$readyExe = Join-Path $PSScriptRoot "dist\RobloxVersionManager-new.exe"
Copy-Item -LiteralPath $newExe -Destination $readyExe -Force

Write-Host "Built executable: dist\RobloxVersionManager-new.exe"
