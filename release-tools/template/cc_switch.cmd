@echo off
setlocal
if not exist "%~dp0cc-switch\cc-switch.exe" (
  echo Missing CC Switch. Extract the entire ZIP again.
  pause
  exit /b 1
)
start "" /D "%~dp0cc-switch" "%~dp0cc-switch\cc-switch.exe"
exit /b 0
