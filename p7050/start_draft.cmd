@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

cscript //nologo "%SCRIPT_DIR%\start_draft_hidden.vbs"
exit /b %ERRORLEVEL%
