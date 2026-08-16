@echo off
setlocal

REM Downloads the official Inno Setup 6 installer without opening a browser.
set "INNO_VERSION=6.7.3"
set "INNO_FILE=%TEMP%\innosetup-%INNO_VERSION%.exe"
set "INNO_URL=https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe"
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"

if not exist "%ISCC%" (
  echo Downloading Inno Setup %INNO_VERSION% from the official GitHub release...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; (New-Object Net.WebClient).DownloadFile('%INNO_URL%', '%INNO_FILE%')"
  if errorlevel 1 (
    echo Download failed. Confirm this Windows 7 computer has TLS 1.2 and Internet access.
    exit /b 1
  )
  "%INNO_FILE%" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-
  if errorlevel 1 exit /b 1
)

call scripts\build-windows.bat
if errorlevel 1 exit /b 1

if not exist "%ISCC%" (
  echo Inno Setup installed but ISCC.exe was not found. Restart Command Prompt and try again.
  exit /b 1
)
"%ISCC%" installer\serial-protocol-translator.iss
if errorlevel 1 exit /b 1

echo.
echo Installer built: dist-installer\Serial-Protocol-Translator-Setup.exe
endlocal
