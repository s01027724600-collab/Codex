@echo off
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -Command "$self=$PID; Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $self -and $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -like '*touchpad_gateway.py*' -and $_.CommandLine -like '*p7050*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
exit /b %ERRORLEVEL%
