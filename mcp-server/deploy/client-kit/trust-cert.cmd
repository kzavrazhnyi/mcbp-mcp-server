@echo off
setlocal

set "CER=%~1"
if "%CER%"=="" set "CER=%~dp0mcbp-mcp.cer"

echo === Trust the mcbp-ai server certificate ===
echo Certificate: %CER%
echo.

net session >nul 2>&1
if errorlevel 1 (
  echo ERROR: administrator rights are required.
  echo Right-click this file and choose "Run as administrator".
  goto :fail
)

if not exist "%CER%" (
  echo ERROR: certificate file not found.
  echo Put mcbp-mcp.cer next to this script, or pass its path as the first argument.
  goto :fail
)

certutil -addstore -f Root "%CER%"
if errorlevel 1 (
  echo ERROR: certutil could not add the certificate.
  goto :fail
)

echo.
echo === Done ===
echo The certificate is now in Trusted Root Certification Authorities.
echo.
pause
exit /b 0

:fail
echo.
echo === FAILED ===
pause
exit /b 1
