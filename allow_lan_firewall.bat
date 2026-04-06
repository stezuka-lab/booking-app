@echo off
setlocal
set "RULE_NAME=booking-app-8000-private"

echo Adding inbound Windows Firewall rule for TCP 8000 on Private networks...
netsh advfirewall firewall add rule name="%RULE_NAME%" dir=in action=allow protocol=TCP localport=8000 profile=private >nul
if errorlevel 1 (
  echo Failed to add the firewall rule.
  echo Run this file as Administrator and try again.
  pause
  exit /b 1
)

echo Firewall rule added: %RULE_NAME%
echo Other PCs on the same private network can now connect to port 8000.
pause
endlocal
