@echo off
cd /d "%~dp0"

set "PY=python"
python --version >nul 2>&1
if errorlevel 1 (
  set "PY=py"
  py --version >nul 2>&1
  if errorlevel 1 (
    echo ERROR: Python not found.
    pause
    exit /b 1
  )
)

echo pip install -r requirements.txt
"%PY%" -m pip install -r "%~dp0requirements.txt"
echo Exit code: %ERRORLEVEL%
pause
