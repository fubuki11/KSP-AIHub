@echo off
setlocal
powershell.exe -NoProfile -File "%~dp0scripts\install-local.ps1" %*
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo Installation did not complete. Read the error above; existing settings were preserved.
pause
exit /b %RESULT%
