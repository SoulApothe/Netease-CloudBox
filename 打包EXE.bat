@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Build CloudBox EXE

echo.
echo   Building a standalone EXE (optional, this may take a few minutes).
echo   The .venv launcher is recommended for daily use.
echo.

set "VENV=%~dp0.venv"
set "PY=%VENV%\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -m pip install --disable-pip-version-check -i https://mirrors.aliyun.com/pypi/simple/ pyinstaller
if errorlevel 1 (
  echo   [ERROR] Failed to install PyInstaller.
  pause
  exit /b 1
)

echo   Packaging ...
"%PY%" -m PyInstaller app.spec --noconfirm --clean
if errorlevel 1 (
  echo   [ERROR] Build failed.
  pause
  exit /b 1
)

echo   Assembling the distribution folder ...
"%PY%" pack_dist.py
if errorlevel 1 (
  echo   [ERROR] Failed to assemble the distribution folder.
  pause
  exit /b 1
)

echo.
echo   Done: dist\CloudBox.exe
echo   Release folder refreshed: release-package\ (exe + cookie.example.txt)
echo   Local settings.json / cloudbox.db / music / cookie.txt are NOT included.
echo.
pause
