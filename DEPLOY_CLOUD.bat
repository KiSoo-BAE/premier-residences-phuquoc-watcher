@echo off
setlocal
cd /d "%~dp0"
title Premier Residences Cloud Deploy
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy_cloud.ps1"
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
  echo [ERROR] Cloud deployment did not finish.
) else (
  echo [OK] Cloud deployment finished.
)
echo.
pause
exit /b %RC%
