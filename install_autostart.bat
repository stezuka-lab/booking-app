@echo off
REM Register run_dev.bat in Windows Startup folder (runs when you sign in to Windows)
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\create_startup_shortcut.ps1" -Root "%~dp0"

echo.
echo Done. Sign out and sign in again, or restart PC. Then open http://127.0.0.1:8000/app
echo To remove: run uninstall_autostart.bat
pause
