@echo off
setlocal
cd /d "%~dp0"
title Premier Residences Cloud Deploy
call "%~dp0DEPLOY_CLOUD.bat"
exit /b %ERRORLEVEL%
