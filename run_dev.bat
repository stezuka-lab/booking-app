@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM Use standard Python under LocalAppData if present. Avoid parentheses in REM lines.
REM Do not use goto inside for-do blocks in batch files.
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
      echo Tip: add python.exe to PATH or install from Microsoft Store.
      pause
      exit /b 1
    )
  )
)

echo Using: "!PY!"
"!PY!" --version

echo.
echo [1/2] pip install -r requirements.txt
"!PY!" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo pip install failed.
  pause
  exit /b 1
)

echo.
echo [2/2] Open http://127.0.0.1:8000/app when ready. Stop: close window or Ctrl+C
echo Booking jobs: embedded scheduler OFF. Run run_jobs.bat in another window when needed.
echo.
start "" cmd /c "ping -n 6 127.0.0.1 >nul && start http://127.0.0.1:8000/app"

set "BOOKING_JOBS_EMBEDDED=false"
"!PY!" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
echo.
echo Server stopped.
pause
endlocal
