@echo off
setlocal

REM Build the application bundle first, then package it with Inno Setup 6.
cd /d "%~dp0.."
call scripts\build-windows.bat
if errorlevel 1 exit /b 1

where ISCC >nul 2>nul
if errorlevel 1 (
  echo Inno Setup 6 is required. Install it from https://jrsoftware.org/isdl.php
  exit /b 1
)

ISCC installer\serial-protocol-translator.iss
if errorlevel 1 exit /b 1

echo.
echo Installer built in dist-installer\Serial-Protocol-Translator-Setup.exe
endlocal
