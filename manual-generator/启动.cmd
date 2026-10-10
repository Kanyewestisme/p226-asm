@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-app.ps1" %*
set "TASK_EXIT_CODE=%ERRORLEVEL%"
if not "%TASK_EXIT_CODE%"=="0" (
  echo.
  echo Application startup failed. See the message above.
  pause
)
exit /b %TASK_EXIT_CODE%
