@echo off
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_exe.ps1"

if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

echo Build complete.
pause
