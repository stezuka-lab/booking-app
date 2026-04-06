@echo off
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\AItest Reservation Server.lnk"
if exist "%LNK%" (
  del "%LNK%"
  echo Removed: %LNK%
) else (
  echo Shortcut not found: %LNK%
)
pause
