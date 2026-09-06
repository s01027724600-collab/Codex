@echo off
setlocal

set "LINK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\RemoteTouchpad-7050.lnk"
if exist "%LINK%" del /f /q "%LINK%"
exit /b %ERRORLEVEL%
