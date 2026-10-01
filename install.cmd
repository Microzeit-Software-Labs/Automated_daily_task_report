@echo off
rem Installs Interlock (or brings an existing install up to date). Double-click it,
rem or run it from a terminal; options go straight through, e.g.  install.cmd -CheckOnly
rem See install.ps1 for what it does. It is safe to run again.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "code=%ERRORLEVEL%"

if not "%code%"=="0" (
    echo.
    echo The installer stopped ^(exit code %code%^). Scroll up to see why.
)

rem Keep the window open when it was started by double-clicking.
echo %cmdcmdline% | find /i "/c" >nul && (
    echo.
    pause
)
exit /b %code%
