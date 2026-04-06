@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM Same Python discovery as run_dev.bat. No reload; listen on all interfaces.
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
echo [1/2] pip install -r requirements.txt
"!PY!" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo pip install failed.
  pause
  exit /b 1
)

set "LAN_IP="
for /f "tokens=2 delims=:" %%I in ('ipconfig ^| findstr /R /C:"IPv4 Address" /C:"IPv4 アドレス"') do (
  if not defined LAN_IP (
    set "LAN_IP=%%I"
  )
)
if defined LAN_IP set "LAN_IP=!LAN_IP: =!"
if defined LAN_IP (
  set "PUBLIC_BASE_URL=http://!LAN_IP!:8000"
  set "GOOGLE_OAUTH_REDIRECT_URI=http://!LAN_IP!:8000/api/booking/oauth/google/callback"
  if not defined BOOKING_SESSION_SECRET set "BOOKING_SESSION_SECRET=local-lan-session-secret-change-me"
  set "BOOKING_SEED_DEMO=false"
)

echo.
echo [2/2] Server on 0.0.0.0:8000 - no auto-reload. Stop with Ctrl+C.
echo Open from this machine: http://127.0.0.1:8000/app
if defined LAN_IP (
  echo Open from another PC on the same network: http://!LAN_IP!:8000/app
  echo PUBLIC_BASE_URL is set for this session: !PUBLIC_BASE_URL!
  echo GOOGLE_OAUTH_REDIRECT_URI is set for this session: !GOOGLE_OAUTH_REDIRECT_URI!
  echo BOOKING_SESSION_SECRET is set for this session.
  echo BOOKING_SEED_DEMO is set to false for this session.
) else (
  echo WARNING: Could not detect a LAN IPv4 address automatically.
  echo Other PCs may not be able to open this app until a LAN IP is set.
)
echo If needed, run allow_lan_firewall.bat as Administrator for Windows Firewall.
echo Booking jobs: embedded scheduler OFF. Run run_jobs.bat in another window.
echo.
set "BOOKING_JOBS_EMBEDDED=false"
"!PY!" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
echo.
echo Server stopped.
pause
endlocal
