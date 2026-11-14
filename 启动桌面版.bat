@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title CloudBox - NetEase Music Desktop

echo.
echo   ==========================================================
echo     CloudBox / NetEase Cloud Music Desktop
echo   ==========================================================
echo.

set "PYEXE="
where python >nul 2>nul
if not errorlevel 1 set "PYEXE=python"
if not defined PYEXE (
  where py >nul 2>nul
  if not errorlevel 1 set "PYEXE=py -3"
)
if not defined PYEXE (
  echo   [ERROR] Python 3.10+ is required but not found.
  echo           Please install from https://www.python.org/downloads/
  echo           Remember to check "Add Python to PATH" during setup.
  echo.
  pause
  exit /b 1
)

set "VENV=%~dp0.venv"
if not exist "%VENV%\Scripts\python.exe" (
  echo   [1/3] Creating local environment ...
  %PYEXE% -m venv "%VENV%"
  if not exist "%VENV%\Scripts\python.exe" (
    echo   [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
  )
)

set "PY=%VENV%\Scripts\python.exe"
"%PY%" -c "import flask, requests, cryptography, mutagen, qrcode, webview" >nul 2>nul
if errorlevel 1 (
  echo   [2/3] Installing dependencies ^(first run only, please wait^) ...
  "%PY%" -m pip install --disable-pip-version-check -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
  if errorlevel 1 (
    echo          Mirror failed, retrying with default index ...
    "%PY%" -m pip install --disable-pip-version-check -r requirements.txt
  )
)

echo   [3/3] Launching desktop window ...
echo.
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
"%PY%" desktop.py %*
if errorlevel 1 (
  echo.
  echo   [ERROR] Application stopped with an error.
  echo           Check music_api.log in this folder for details.
  echo.
  pause
)
endlocal
