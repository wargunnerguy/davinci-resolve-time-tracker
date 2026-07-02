@echo off
setlocal
title DaVinci Resolve Time Tracker - Installer

echo.
echo   DaVinci Resolve Time Tracker - Installer
echo   ========================================
echo.

set "SRC=%~dp0ResolveTimeTracker.py"
if not exist "%SRC%" (
  echo   ERROR: ResolveTimeTracker.py was not found next to this installer.
  echo   Keep install.bat and ResolveTimeTracker.py together in one folder.
  echo.
  pause
  exit /b 1
)

set "DEST=%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility"
if not exist "%DEST%" mkdir "%DEST%" 2>nul

copy /Y "%SRC%" "%DEST%\ResolveTimeTracker.py" >nul
if errorlevel 1 (
  echo   ERROR: Could not copy into Resolve's Scripts folder:
  echo   %DEST%
  echo   Is DaVinci Resolve installed for this user?
  echo.
  pause
  exit /b 1
)
echo   [OK] Installed into Resolve's Scripts\Utility folder.
echo.

rem --- best-effort check for a system Python 3 with Tkinter ---
set "PYEXE="
call :findpy py
if not defined PYEXE call :findpy python
if not defined PYEXE if exist "C:\Python314\python.exe" set "PYEXE=C:\Python314\python.exe"

if defined PYEXE (
  echo   [OK] Found Python with Tkinter: %PYEXE%
) else (
  echo   [!] Could not find Python 3 with Tkinter.
  echo       Install it from https://www.python.org/downloads/
  echo       ^(keep the default options - Tkinter is included^). The tracker
  echo       needs it to run.
)
echo.
echo   ------------------------------------------------------------
echo   One more step, inside DaVinci Resolve ^(one time^):
echo     Preferences ^> System ^> General ^>
echo       "External scripting using" = Local
echo     then restart Resolve.
echo.
echo   Launch it from:  Workspace ^> Scripts ^> ResolveTimeTracker
echo   ------------------------------------------------------------
echo.
pause
exit /b 0

:findpy
rem %1 = command to try; sets PYEXE only if it exists AND has Tkinter
where %1 >nul 2>nul || exit /b 0
%1 -c "import tkinter" >nul 2>nul || exit /b 0
for /f "delims=" %%I in ('%1 -c "import sys;print(sys.executable)"') do set "PYEXE=%%I"
exit /b 0
