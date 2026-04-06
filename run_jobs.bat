@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PY="
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "PY=%LocalAppData%\Programs\Python\Python311\python.exe"
if not defined PY if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set "PY=%LocalAppData%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LocalAppData%\Programs\Python\Python310\python.exe" set "PY=%LocalAppData%\Programs\Python\Python310\python.exe"

if not defined PY (
  set "PY=python"
  "!PY!" --version >nul 2>&1
  if errorlevel 1 (
    set "PY=py"
    "!PY!" --version >nul 2>&1
    if errorlevel 1 (
      echo ERROR: Python not found. Install from https://www.python.org/downloads/
      pause
      exit /b 1
    )
  )
)

echo Using: "!PY!"
"!PY!" --version
echo.
echo Job runner starting. Embedded scheduler should be OFF in the web server process.
echo Stop with Ctrl+C.
echo.
"!PY!" -m app.booking.job_runner loop --interval 60
echo.
echo Job runner stopped.
pause
endlocal
