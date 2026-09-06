@echo off
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0gateway_runtime.ps1" -Command initialize -NoPause
set "DEPLOY_EXIT=%ERRORLEVEL%"
if not "%DEPLOY_EXIT%"=="0" echo Deployment is NOT complete. Read FAILED items above. Correct them and run this file again.
pause
exit /b %DEPLOY_EXIT%
