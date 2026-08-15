@echo off
setlocal

REM Build the main GUI on Windows. Run this file from a Command Prompt or by
REM double-clicking it after Python 3.10+ is installed.
cd /d "%~dp0.."

py -m pip install -r requirements-build.txt
if errorlevel 1 exit /b 1

py -m PyInstaller --noconfirm --clean --windowed --onedir ^
  --name "GasWorks-ProLab-Serial-Translator" ^
  --collect-all serial ^
  --collect-all pystray ^
  --collect-all PIL ^
  translator.py
if errorlevel 1 exit /b 1

echo.
echo Build complete:
echo   dist\GasWorks-ProLab-Serial-Translator\GasWorks-ProLab-Serial-Translator.exe
endlocal
