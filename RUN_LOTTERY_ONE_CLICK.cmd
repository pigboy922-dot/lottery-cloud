@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title Lottery One Click Update - Safe

echo ========================================
echo LOTTERY ONE CLICK UPDATE - SAFE
echo Folder: %cd%
echo ========================================
echo.

set "PYEXE="
where py >nul 2>nul
if %errorlevel%==0 set "PYEXE=py -3"
if "%PYEXE%"=="" (
  where python >nul 2>nul
  if %errorlevel%==0 set "PYEXE=python"
)
if "%PYEXE%"=="" (
  echo [ERROR] Python not found. Install Python 3 first.
  goto END
)

echo [STEP 1] Python: %PYEXE%
%PYEXE% --version
if errorlevel 1 goto END

echo.
echo [STEP 2] Install/check packages...
%PYEXE% -m pip install --upgrade pip
%PYEXE% -m pip install -r requirements.txt
if errorlevel 1 goto END

echo.
echo [STEP 3] Update lottery data and build dashboard...
set "PYTHONIOENCODING=utf-8"
%PYEXE% scripts\lottery_auto_update_full.py --weekly
if errorlevel 1 (
  echo [ERROR] Update failed.
  if exist "output\lottery_error.txt" type "output\lottery_error.txt"
  goto END
)

echo.
echo [STEP 4] Open dashboard...
if exist "output\lottery_final_dashboard.html" start "" "output\lottery_final_dashboard.html"

echo.
echo [DONE]
:END
echo.
echo Press any key to close.
pause >nul
