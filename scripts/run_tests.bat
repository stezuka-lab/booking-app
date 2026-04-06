@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
echo Running pytest...
python -m pytest -q %*
echo Exit code: %ERRORLEVEL%
pause
