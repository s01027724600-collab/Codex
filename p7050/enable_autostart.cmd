@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LINK=%STARTUP%\RemoteTouchpad-7050.lnk"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$shell=New-Object -ComObject WScript.Shell;" ^
  "$shortcut=$shell.CreateShortcut('%LINK%');" ^
  "$shortcut.TargetPath=\"$env:WINDIR\System32\wscript.exe\";" ^
  "$shortcut.Arguments='\"%SCRIPT_DIR%\start_draft_hidden.vbs\"';" ^
  "$shortcut.WorkingDirectory='%SCRIPT_DIR%';" ^
  "$shortcut.Description='Remote Touchpad Gateway 7050';" ^
  "$shortcut.Save()"

if errorlevel 1 exit /b %ERRORLEVEL%
cscript //nologo "%SCRIPT_DIR%\start_draft_hidden.vbs"
exit /b %ERRORLEVEL%
